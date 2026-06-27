import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request

from .proxy import filter_config_by_protocol, passthrough


logger = logging.getLogger("gpt_proxy")


@dataclass(frozen=True)
class V1RouteServices:
    read_v1_json_body: Callable[[Request], Awaitable[dict[str, Any]]]
    load_config: Callable[[], dict[str, Any]]
    authorize_v1_access: Callable[[Request, str | None], dict[str, Any] | None]
    enforce_rate_limit: Callable[[Request], None]
    model_allowed_for_client_key: Callable[[dict[str, Any], str | None], bool]
    fetch_provider_models: Callable[[dict[str, Any]], Awaitable[list[dict[str, Any]]]]
    proxy_services: Callable[[], Any]


def normalize_model_entries(models: Any) -> list[dict[str, Any]]:
    if not isinstance(models, list):
        return []
    normalized = []
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = str(model.get("id") or model.get("name") or "").removeprefix("models/").strip()
        if not model_id:
            continue
        item = dict(model)
        item["id"] = model_id
        item.setdefault("object", "model")
        normalized.append(item)
    return normalized


def register_v1_routes(app: FastAPI, services: V1RouteServices) -> None:
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        body = await services.read_v1_json_body(request)
        config = filter_config_by_protocol(services.load_config(), {"openai", "domestic"})
        if not config["providers"]:
            raise HTTPException(
                status_code=503,
                detail="No OpenAI or domestic model providers configured. Please add at least one provider with protocol 'openai' or 'domestic'."
            )
        return await passthrough(body, config, request, services.proxy_services(), "auto", "/chat/completions", "/v1/chat/completions")

    @app.post("/v1/responses")
    async def responses(request: Request):
        body = await services.read_v1_json_body(request)
        config = filter_config_by_protocol(services.load_config(), {"openai"})
        if not config["providers"]:
            raise HTTPException(
                status_code=503,
                detail="No OpenAI providers configured. Please add at least one provider with protocol 'openai'."
            )
        return await passthrough(body, config, request, services.proxy_services(), "openai", "/responses", "/v1/responses")

    @app.post("/v1/messages")
    async def messages(request: Request):
        body = await services.read_v1_json_body(request)
        config = filter_config_by_protocol(services.load_config(), {"claude"})
        if not config["providers"]:
            raise HTTPException(
                status_code=503,
                detail="No Claude providers configured. Please add at least one provider with protocol 'claude' and base_url 'https://api.anthropic.com/v1'."
            )
        return await passthrough(body, config, request, services.proxy_services(), "claude", "/messages", "/v1/messages")

    @app.post("/v1beta/models/{rest:path}")
    async def gemini_generate(rest: str, request: Request):
        body = await services.read_v1_json_body(request)
        config = filter_config_by_protocol(services.load_config(), {"gemini"})
        if not config["providers"]:
            raise HTTPException(
                status_code=503,
                detail="No Gemini providers configured. Please add at least one provider with protocol 'gemini' and base_url 'https://generativelanguage.googleapis.com/v1beta'."
            )
        if ":" not in rest:
            raise HTTPException(status_code=404, detail="Invalid Gemini endpoint. Expected format: /v1beta/models/{model}:generateContent")
        model, verb = rest.rsplit(":", 1)
        if verb not in {"generateContent", "streamGenerateContent"}:
            raise HTTPException(status_code=404, detail=f"Unsupported Gemini verb: {verb}. Supported verbs: generateContent, streamGenerateContent")
        services.authorize_v1_access(request, model)
        if verb == "streamGenerateContent" or request.query_params.get("alt") == "sse":
            body["stream"] = True
        path_suffix = f"/models/{model}:{verb}"
        return await passthrough(
            body,
            config,
            request,
            services.proxy_services(),
            "gemini",
            "/models/{model}:" + verb,
            f"/v1beta/models/{rest}",
            path_model=model,
        )

    @app.get("/v1/models")
    async def list_models(request: Request) -> dict[str, Any]:
        client_key = services.authorize_v1_access(request, None)
        services.enforce_rate_limit(request)
        config = services.load_config()
        seen = set()
        models = []
        for provider in config["providers"]:
            try:
                provider_models = await services.fetch_provider_models(provider)
            except Exception as exc:
                logger.info("provider=%s status=model_fetch_error detail=%s", provider.get("name", "unknown"), exc)
                provider_models = []
            provider_models = normalize_model_entries(provider_models)
            if not provider_models and provider.get("model"):
                provider_models = normalize_model_entries([{"id": provider["model"], "object": "model"}])
            for model in provider_models:
                model_id = model["id"]
                if client_key and not services.model_allowed_for_client_key(client_key, model_id):
                    continue
                if model_id in seen:
                    continue
                seen.add(model_id)
                item = dict(model)
                item.setdefault("object", "model")
                item["owned_by"] = item.get("owned_by") or provider["name"]
                models.append(item)
        return {"object": "list", "data": models}
