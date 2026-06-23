import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request

from ._config import (
    apply_env_overrides,
    client_key_entries,
    client_key_model_rules,
    editable_client_key,
    editable_provider,
    normalize_config_payload,
    provider_api_keys,
    provider_with_safe_priority,
    safe_api_key_env,
    safe_bool,
    safe_model_aliases,
    safe_provider_base_url,
    safe_provider_model,
    safe_provider_name,
    safe_provider_priority,
    safe_secret_value,
)
from ._providers import provider_presets, protocol_catalog, provider_protocol
from ._routing import build_provider_routing_profile, order_providers_for_request
from ._state import summarize_request_stats
from .proxy import filter_config_by_protocol
from .routes_v1 import normalize_model_entries


logger = logging.getLogger("gpt_proxy")


@dataclass(frozen=True)
class AdminRouteServices:
    require_proxy_access: Callable[[Request], None]
    read_json_body: Callable[[Request], Awaitable[Any]]
    load_config: Callable[[], dict[str, Any]]
    load_raw_config: Callable[[], dict[str, Any]]
    load_state: Callable[[], dict[str, Any]]
    save_state: Callable[[dict[str, Any]], None]
    write_config_file: Callable[[dict[str, Any]], None]
    check_provider: Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]]
    fetch_provider_models: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]]
    enabled_client_key_count: Callable[[dict[str, Any]], int]
    management_auth_mode: Callable[[], str]
    v1_auth_mode: Callable[[dict[str, Any]], str]
    provider_state_entry: Callable[[dict[str, Any], str], dict[str, Any]]
    provider_health: Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]
    request_log_entries: Callable[[dict[str, Any], int | None], list[dict[str, Any]]]
    proxy_access_token_enabled: Callable[[], bool]
    config_encryption_enabled: Callable[[], bool]
    rate_limit_per_minute: Callable[[], int]
    max_request_bytes: Callable[[], int]
    key_cooldown_seconds: Callable[[], int]
    request_log_limit: int


def redacted_config_export(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "redacted": True,
        "default_model": config.get("default_model", "gpt-3.5-turbo"),
        "providers": [
            {
                "name": safe_provider_name(provider),
                "protocol": provider_protocol(provider),
                "base_url": safe_provider_base_url(provider),
                "model": safe_provider_model(provider) or config.get("default_model", "gpt-3.5-turbo"),
                "priority": safe_provider_priority(provider),
                "enabled": safe_bool(provider.get("enabled"), True),
                "api_key": "",
                "api_keys": [],
                "api_key_env": safe_api_key_env(provider),
                "has_api_key": bool(provider_api_keys(provider)),
                "key_count": len(provider_api_keys(provider)),
                "use_curl": safe_bool(provider.get("use_curl"), False),
                "model_aliases": safe_model_aliases(provider),
            }
            for provider in sorted(config.get("providers", []), key=safe_provider_priority)
        ],
        "client_keys": [
            {
                "id": client_key.get("id", ""),
                "label": client_key.get("label", ""),
                "key": "",
                "has_key": bool(safe_secret_value(client_key.get("key", ""))),
                "enabled": safe_bool(client_key.get("enabled"), True),
                "allowed_models": client_key_model_rules(client_key, "allowed_models"),
                "excluded_models": client_key_model_rules(client_key, "excluded_models"),
            }
            for client_key in client_key_entries(config)
        ],
    }


ROUTING_PREVIEW_TARGETS: dict[str, set[str]] = {
    "chat": {"openai", "domestic"},
    "openai": {"openai"},
    "responses": {"openai"},
    "domestic": {"domestic"},
    "claude": {"claude"},
    "gemini": {"gemini"},
}


def routing_preview_skip_reason(provider: dict[str, Any], protocols: set[str]) -> tuple[str, str] | None:
    protocol = provider_protocol(provider)
    if not safe_bool(provider.get("enabled"), True):
        return "disabled", "Provider is disabled and will not participate in this preview."
    if not provider.get("name"):
        return "missing_name", "Provider name is missing."
    if not provider.get("base_url"):
        return "missing_base_url", "Provider Base URL is missing."
    if not provider_api_keys(provider):
        return "missing_key", "Provider API key is missing."
    if protocol not in protocols:
        return "protocol_mismatch", f"Protocol {protocol} does not match this preview target."
    return None


def routing_preview_skipped_providers(raw_providers: list[dict[str, Any]], protocols: set[str]) -> list[dict[str, Any]]:
    skipped: list[dict[str, Any]] = []
    for provider in sorted(raw_providers, key=safe_provider_priority):
        provider_with_env = provider_with_safe_priority(apply_env_overrides(provider))
        skip = routing_preview_skip_reason(provider_with_env, protocols)
        if skip is None:
            continue
        reason, message = skip
        skipped.append(
            {
                "name": provider_with_env.get("name") or "unnamed provider",
                "protocol": provider_protocol(provider_with_env),
                "priority": safe_provider_priority(provider_with_env),
                "reason": reason,
                "message": message,
            }
        )
    return skipped


def register_admin_routes(app: FastAPI, services: AdminRouteServices) -> None:
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok"}

    @app.get("/health/detailed")
    def health_detailed() -> dict[str, Any]:
        config = services.load_config()
        catalog = protocol_catalog()
        protocols = {protocol: 0 for protocol in catalog}

        for provider in config["providers"]:
            protocol = provider_protocol(provider)
            if protocol in protocols:
                protocols[protocol] += 1

        return {
            "status": "ok",
            "protocols": protocols,
            "protocol_catalog": catalog,
            "total_providers": len(config["providers"]),
            "endpoints": {
                "openai_chat": "/v1/chat/completions (OpenAI & domestic)",
                "openai_responses": "/v1/responses (OpenAI only)",
                "claude_messages": "/v1/messages (Claude only)",
                "gemini_generate": "/v1beta/models/{model}:generateContent (Gemini only)",
            },
        }

    @app.get("/api/protocols")
    def protocols_endpoint(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        config = services.load_config()
        counts = {protocol: 0 for protocol in protocol_catalog()}
        for provider in config["providers"]:
            protocol = provider_protocol(provider)
            if protocol in counts:
                counts[protocol] += 1
        return {
            "protocols": [
                {"name": name, "count": counts.get(name, 0), **info}
                for name, info in protocol_catalog().items()
            ]
        }

    @app.get("/api/provider-presets")
    def provider_presets_endpoint(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        return {"presets": provider_presets()}

    @app.get("/api/config")
    def get_config(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        config = services.load_raw_config()
        state = services.load_state()
        return {
            "default_model": config.get("default_model", "gpt-3.5-turbo"),
            "providers": [
                editable_provider(
                    provider_with_safe_priority(provider),
                    state,
                    config.get("default_model", "gpt-3.5-turbo"),
                )
                for provider in sorted(config.get("providers", []), key=safe_provider_priority)
            ],
            "client_keys": [
                editable_client_key(client_key)
                for client_key in client_key_entries(config)
            ],
            "security": {
                "proxy_access_token_enabled": services.proxy_access_token_enabled(),
                "management_auth_mode": services.management_auth_mode(),
                "v1_auth_mode": services.v1_auth_mode(config),
                "enabled_client_key_count": services.enabled_client_key_count(config),
                "config_encryption_enabled": services.config_encryption_enabled(),
                "rate_limit_per_minute": services.rate_limit_per_minute(),
                "max_request_bytes": services.max_request_bytes(),
                "key_cooldown_seconds": services.key_cooldown_seconds(),
            },
        }

    @app.post("/api/config")
    async def save_config(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        try:
            payload = await services.read_json_body(request)
            config = normalize_config_payload(payload, services.load_raw_config())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        services.write_config_file(config)
        return get_config(request)

    @app.delete("/api/providers/{provider_name}")
    def delete_provider(provider_name: str, request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        config = services.load_raw_config()
        providers = config.get("providers", [])
        remaining_providers = [
            provider for provider in providers if provider.get("name") != provider_name
        ]
        if len(remaining_providers) == len(providers):
            raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")

        config["providers"] = remaining_providers
        services.write_config_file(config)

        state = services.load_state()
        if provider_name in state:
            state.pop(provider_name, None)
            services.save_state(state)

        return get_config(request)

    @app.get("/api/config/export")
    def export_config(request: Request, redacted: bool = False) -> dict[str, Any]:
        services.require_proxy_access(request)
        config = services.load_raw_config()
        if redacted:
            return redacted_config_export(config)
        return config

    @app.post("/api/config/import")
    async def import_config(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        try:
            payload = await services.read_json_body(request)
            if isinstance(payload, dict) and payload.get("redacted") is True:
                raise ValueError("Redacted config exports cannot be imported because secrets are omitted")
            config = normalize_config_payload(payload, {"providers": []})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        services.write_config_file(config)
        return get_config(request)

    @app.get("/api/providers")
    def provider_status(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        config = services.load_config()
        state = services.load_state()
        providers = []
        for provider in config["providers"]:
            provider_state = services.provider_state_entry(state, provider["name"])
            providers.append(
                {
                    "name": provider["name"],
                    "protocol": provider_protocol(provider),
                    "base_url": provider["base_url"],
                    "model": provider.get("model", config["default_model"]),
                    "priority": provider.get("priority", 1000),
                    "enabled": safe_bool(provider.get("enabled"), True),
                    "key_count": len(provider_api_keys(provider)),
                    "calls": provider_state.get("calls", 0),
                    "last_remaining": provider_state.get("last_remaining"),
                    "health": services.provider_health(provider["name"], provider_state, state),
                }
            )
        return {"providers": providers}

    @app.get("/api/requests")
    def recent_requests(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        state = services.load_state()
        return {"requests": services.request_log_entries(state, services.request_log_limit)}

    @app.get("/api/stats")
    def request_stats(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        return summarize_request_stats(services.load_state())

    @app.delete("/api/observability")
    def clear_observability(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        state = services.load_state()
        requests = state.get("_requests")
        cleared_requests = len(requests) if isinstance(requests, list) else 0
        cleared_stats = "_stats" in state
        state["_requests"] = []
        state.pop("_stats", None)
        services.save_state(state)
        return {
            "cleared": {
                "requests": cleared_requests,
                "stats": cleared_stats,
            },
            "requests": [],
            "stats": summarize_request_stats(state),
        }

    @app.get("/api/routing/preview")
    def routing_preview(request: Request, target: str = "chat") -> dict[str, Any]:
        services.require_proxy_access(request)
        target_key = (target or "chat").strip().lower()
        protocols = ROUTING_PREVIEW_TARGETS.get(target_key)
        if protocols is None:
            allowed = ", ".join(sorted(ROUTING_PREVIEW_TARGETS))
            raise HTTPException(status_code=400, detail=f"Unknown routing preview target '{target}'. Expected one of: {allowed}")

        raw_config = services.load_raw_config()
        config = filter_config_by_protocol(services.load_config(), protocols)
        state = services.load_state()
        now = time.time()
        candidates = order_providers_for_request(config["providers"], state, now=now)
        selected_provider = candidates[0].provider["name"] if candidates else None
        status = "ready" if selected_provider else "empty"
        if selected_provider:
            message = candidates[0].message or f"下一次 {target_key} 请求会优先尝试 {selected_provider}。"
        else:
            message = f"暂无可用于 {target_key} 路由预览的 provider。"
        profile_by_name = {
            candidate.provider["name"]: build_provider_routing_profile(candidate.provider, state, index, now=now)
            for index, candidate in enumerate(candidates)
        }

        return {
            "target": target_key,
            "status": status,
            "message": message,
            "protocols": sorted(protocols),
            "selected_provider": selected_provider,
            "candidates": [
                {
                    "name": candidate.provider["name"],
                    "protocol": provider_protocol(candidate.provider),
                    "priority": candidate.provider.get("priority", 1000),
                    "reason": candidate.reason,
                    "routing_reason": candidate.reason,
                    "message": candidate.message,
                    "all_keys_cooling": bool(profile_by_name[candidate.provider["name"]] and profile_by_name[candidate.provider["name"]].all_keys_cooling),
                    "cooldown_seconds": profile_by_name[candidate.provider["name"]].cooldown_seconds if profile_by_name[candidate.provider["name"]] else 0,
                    "degraded": bool(profile_by_name[candidate.provider["name"]] and profile_by_name[candidate.provider["name"]].degraded),
                    "success_rate": profile_by_name[candidate.provider["name"]].success_rate if profile_by_name[candidate.provider["name"]] else None,
                    "avg_latency_ms": round(profile_by_name[candidate.provider["name"]].avg_latency_ms, 2) if profile_by_name[candidate.provider["name"]] else 0,
                    "key_count": len(provider_api_keys(candidate.provider)),
                }
                for candidate in candidates
            ],
            "skipped_providers": routing_preview_skipped_providers(raw_config.get("providers", []), protocols),
        }

    @app.get("/api/model-coverage")
    async def model_coverage(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        config = services.load_config()
        semaphore = asyncio.Semaphore(6)

        async def provider_coverage(provider: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                try:
                    models = await services.fetch_provider_models(provider)
                    detail = "ok"
                except Exception as exc:
                    models = []
                    detail = str(exc)

                models = normalize_model_entries(models)
                fallback_used = False
                if not models and provider.get("model"):
                    fallback_used = True
                    models = normalize_model_entries([{"id": provider["model"], "object": "model"}])

                model_ids = sorted({model["id"] for model in models})
                return {
                    "name": provider["name"],
                    "protocol": provider_protocol(provider),
                    "priority": provider.get("priority", 1000),
                    "ok": bool(model_ids),
                    "model_count": len(model_ids),
                    "models": model_ids,
                    "fallback_used": fallback_used,
                    "detail": detail if model_ids or not fallback_used else "Using configured default model.",
                }

        providers = await asyncio.gather(*(provider_coverage(provider) for provider in config["providers"]))
        models_by_id: dict[str, dict[str, Any]] = {}
        for provider in providers:
            for model_id in provider["models"]:
                item = models_by_id.setdefault(
                    model_id,
                    {
                        "id": model_id,
                        "providers": [],
                        "protocols": [],
                    },
                )
                item["providers"].append(provider["name"])
                if provider["protocol"] not in item["protocols"]:
                    item["protocols"].append(provider["protocol"])

        return {
            "total_providers": len(providers),
            "unique_model_count": len(models_by_id),
            "providers": providers,
            "models": sorted(models_by_id.values(), key=lambda item: item["id"]),
        }

    @app.post("/api/providers/{provider_name}/check")
    async def provider_check(provider_name: str, request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        config = services.load_config()
        raw_config = services.load_raw_config()
        raw_names = {provider.get("name") for provider in raw_config.get("providers", []) if provider.get("name")}
        for provider in config["providers"]:
            if provider["name"] == provider_name:
                try:
                    result = await services.check_provider(provider, config["default_model"])
                except Exception as exc:
                    logger.info("provider=%s status=check_error detail=%s", provider_name, exc)
                    result = {"ok": False, "status": "check_error", "detail": str(exc)}
                return {"provider": provider_name, **result}
        if provider_name in raw_names:
            return {
                "provider": provider_name,
                "ok": False,
                "status": "no_api_key",
                "detail": "Provider is disabled or missing an API key.",
            }
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' does not exist in config")

    @app.post("/api/providers/check-all")
    async def providers_check_all(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        config = services.load_config()
        semaphore = asyncio.Semaphore(6)

        async def run_check(provider: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                try:
                    result = await services.check_provider(provider, config["default_model"])
                except Exception as exc:
                    logger.info("provider=%s status=check_error detail=%s", provider.get("name", "unknown"), exc)
                    result = {"ok": False, "status": "check_error", "detail": str(exc)}
                return {
                    "provider": provider["name"],
                    "protocol": provider_protocol(provider),
                    "model": provider.get("model", config["default_model"]),
                    **result,
                }

        results = await asyncio.gather(*(run_check(provider) for provider in config["providers"]))
        ok_count = sum(1 for result in results if result.get("ok"))
        return {
            "total": len(results),
            "ok": ok_count,
            "failed": len(results) - ok_count,
            "results": results,
        }

    @app.get("/api/providers/{provider_name}/models")
    async def provider_models_endpoint(provider_name: str, request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        config = services.load_config()
        raw_config = services.load_raw_config()
        raw_names = {provider.get("name") for provider in raw_config.get("providers", []) if provider.get("name")}
        for provider in config["providers"]:
            if provider["name"] == provider_name:
                try:
                    models = normalize_model_entries(await services.fetch_provider_models(provider))
                except Exception as exc:
                    logger.info("provider=%s status=model_fetch_error detail=%s", provider_name, exc)
                    models = []
                    fallback_used = False
                    if provider.get("model"):
                        fallback_used = True
                        models = normalize_model_entries([{"id": provider["model"], "object": "model"}])
                    return {
                        "provider": provider_name,
                        "ok": False,
                        "status": "model_fetch_error",
                        "detail": str(exc),
                        "fallback_used": fallback_used,
                        "models": {"object": "list", "data": models},
                    }
                fallback_used = False
                if not models and provider.get("model"):
                    fallback_used = True
                    models = normalize_model_entries([{"id": provider["model"], "object": "model"}])
                return {
                    "provider": provider_name,
                    "ok": bool(models),
                    "status": 200,
                    "fallback_used": fallback_used,
                    "models": {"object": "list", "data": models},
                }
        if provider_name in raw_names:
            return {
                "provider": provider_name,
                "ok": False,
                "status": "no_api_key",
                "detail": "Provider is disabled or missing an API key.",
                "models": {"object": "list", "data": []},
            }
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")

    @app.post("/api/providers/models/sync")
    async def provider_models_sync(request: Request) -> dict[str, Any]:
        services.require_proxy_access(request)
        config = services.load_config()
        semaphore = asyncio.Semaphore(6)

        async def run_fetch(provider: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                try:
                    models = normalize_model_entries(await services.fetch_provider_models(provider))
                except Exception as exc:
                    fallback_used = False
                    models = []
                    if provider.get("model"):
                        fallback_used = True
                        models = normalize_model_entries([{"id": provider["model"], "object": "model"}])
                    return {
                        "provider": provider["name"],
                        "protocol": provider_protocol(provider),
                        "model": provider.get("model", config["default_model"]),
                        "ok": False,
                        "status": "model_fetch_error",
                        "count": len(models),
                        "models": models,
                        "fallback_used": fallback_used,
                        "detail": str(exc),
                    }

                fallback_used = False
                if not models and provider.get("model"):
                    fallback_used = True
                    models = normalize_model_entries([{"id": provider["model"], "object": "model"}])

                return {
                    "provider": provider["name"],
                    "protocol": provider_protocol(provider),
                    "model": provider.get("model", config["default_model"]),
                    "ok": bool(models),
                    "count": len(models),
                    "models": models,
                    "fallback_used": fallback_used,
                    "detail": "ok" if models else "No model list returned.",
                }

        results = await asyncio.gather(*(run_fetch(provider) for provider in config["providers"]))
        ok_count = sum(1 for result in results if result.get("ok"))
        unique_models: dict[str, dict[str, Any]] = {}
        for result in results:
            for model in result.get("models", []):
                model_id = model.get("id")
                if model_id and model_id not in unique_models:
                    unique_models[model_id] = {
                        "id": model_id,
                        "provider": result["provider"],
                        "protocol": result["protocol"],
                    }

        return {
            "total": len(results),
            "ok": ok_count,
            "failed": len(results) - ok_count,
            "unique_model_count": len(unique_models),
            "models": list(unique_models.values()),
            "results": results,
        }
