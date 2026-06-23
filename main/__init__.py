import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ._auth import (
    authorize_model_for_rules,
    enforce_rate_limit as enforce_request_rate_limit,
    management_auth_mode as auth_management_auth_mode,
    model_allowed_for_rules,
    model_matches_any,
    rate_limit_identity as request_rate_limit_identity,
    request_token,
    require_proxy_access_token,
    secret_matches,
    v1_auth_mode as auth_v1_auth_mode,
)
from ._config import (
    apply_env_overrides,
    client_key_entries,
    client_key_model_rules,
    decrypt_config,
    editable_client_key,
    editable_provider,
    encrypt_config,
    normalize_config_payload,
    provider_api_keys,
    provider_with_safe_priority,
    read_json_file,
    safe_api_key_env,
    safe_bool,
    safe_model_aliases,
    safe_provider_model,
    safe_provider_base_url,
    safe_provider_name,
    safe_provider_priority,
    safe_secret_value,
    write_json_file,
)
from ._providers import (
    _prepare_forward,
    build_forward_headers,
    build_request_body,
    check_provider,
    curl_forward_to_provider,
    curl_stream_request,
    fetch_provider_models,
    forward_to_provider,
    protocol_catalog,
    provider_presets,
    provider_protocol,
    safe_response_detail,
    should_use_curl,
)
from ._routing import RoutingCandidate, build_provider_routing_profile, order_providers_for_request
from ._state import key_fingerprint, record_request_stats, request_log_entry, stats_container, summarize_request_stats

# ---------------------------------------------------------------------------
# Monkeypatchable module-level constants
# ---------------------------------------------------------------------------

def env_int(name: str, default: int) -> int:
    value = os.getenv(name, "")
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = Path(os.getenv("GPT_PROXY_CONFIG", BASE_DIR / "config.json"))
STATE_PATH = Path(os.getenv("GPT_PROXY_STATE", BASE_DIR / "state.json"))
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
REQUEST_LOG_LIMIT = 50
PROXY_ACCESS_TOKEN = os.getenv("GPT_PROXY_ACCESS_TOKEN", "").strip()
CONFIG_ENCRYPTION_SECRET = os.getenv("GPT_PROXY_CONFIG_SECRET", "").strip()
RATE_LIMIT_PER_MINUTE = env_int("GPT_PROXY_RATE_LIMIT_PER_MINUTE", 0)
MAX_REQUEST_BYTES = env_int("GPT_PROXY_MAX_REQUEST_BYTES", 2 * 1024 * 1024)
KEY_COOLDOWN_SECONDS = env_int("GPT_PROXY_KEY_COOLDOWN_SECONDS", 60)
RATE_LIMIT_BUCKETS: dict[str, list[float]] = {}
_state_cache: dict[str, Any] | None = None
_state_cache_path: Path | None = None
_state_write_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="[%(asctime)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("gpt_proxy")

# ---------------------------------------------------------------------------
# HTTP client helpers (monkeypatchable)
# ---------------------------------------------------------------------------

def create_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30.0)


_original_create_http_client = create_http_client


def get_http_client(application: FastAPI) -> httpx.AsyncClient:
    if create_http_client is not _original_create_http_client:
        return create_http_client()
    if not hasattr(application.state, "http_client") or application.state.http_client is None:
        application.state.http_client = create_http_client()
    return application.state.http_client

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

async def _periodic_state_flush() -> None:
    while True:
        await asyncio.sleep(5)
        flush_state_cache()


def flush_state_cache() -> None:
    global _state_cache
    if _state_cache is not None:
        with _state_write_lock:
            write_json_file(STATE_PATH, _state_cache)


def is_json_file_content_error(exc: BaseException) -> bool:
    if isinstance(exc, UnicodeDecodeError):
        return True
    return isinstance(exc.__cause__, (json.JSONDecodeError, UnicodeDecodeError))


@asynccontextmanager
async def lifespan(application: FastAPI):
    task = asyncio.create_task(_periodic_state_flush())
    yield
    task.cancel()
    flush_state_cache()
    client = getattr(application.state, "http_client", None)
    if client is not None:
        await client.aclose()


def load_state() -> dict[str, Any]:
    global _state_cache, _state_cache_path
    if _state_cache is None or _state_cache_path != STATE_PATH:
        try:
            state = read_json_file(STATE_PATH, {})
        except (RuntimeError, UnicodeDecodeError) as exc:
            if not is_json_file_content_error(exc):
                raise
            state = {}
        _state_cache = state if isinstance(state, dict) else {}
        _state_cache_path = STATE_PATH
    return _state_cache


def save_state(state: dict[str, Any]) -> None:
    global _state_cache, _state_cache_path
    _state_cache = state
    _state_cache_path = STATE_PATH
    with _state_write_lock:
        write_json_file(STATE_PATH, state)


def provider_state_entry(state: dict[str, Any], provider_name: str, create: bool = False) -> dict[str, Any]:
    value = state.get(provider_name)
    if isinstance(value, dict):
        return value
    if create:
        value = {"calls": 0, "last_remaining": None}
        state[provider_name] = value
        return value
    return {}


def provider_key_cooldowns(provider_state: dict[str, Any], create: bool = False) -> dict[str, Any]:
    cooldowns = provider_state.get("key_cooldowns")
    if isinstance(cooldowns, dict):
        return cooldowns
    if create or "key_cooldowns" in provider_state:
        provider_state["key_cooldowns"] = {}
        return provider_state["key_cooldowns"]
    return {}


def record_key_cooldown(provider_name: str, api_key: str) -> None:
    if KEY_COOLDOWN_SECONDS <= 0:
        return
    state = load_state()
    provider_state = provider_state_entry(state, provider_name, create=True)
    cooldowns = provider_key_cooldowns(provider_state, create=True)
    cooldowns[key_fingerprint(api_key)] = time.time() + KEY_COOLDOWN_SECONDS
    save_state(state)


def key_attempt_order(provider: dict[str, Any], state: dict[str, Any]) -> list[tuple[int, str]]:
    keys = provider_api_keys(provider)
    if not keys:
        return []
    provider_state = provider_state_entry(state, provider["name"])
    start_index = parse_state_int(provider_state.get("key_index")) % len(keys)
    ordered_indexes = list(range(start_index, len(keys))) + list(range(0, start_index))
    cooldowns = provider_key_cooldowns(provider_state, create=True)
    now = time.time()
    available = []
    cooled = []
    for index in ordered_indexes:
        key = keys[index]
        if float(cooldowns.get(key_fingerprint(key), 0) or 0) > now:
            cooled.append((index, key))
        else:
            available.append((index, key))
    return available or cooled


def record_success(
    provider_name: str,
    response: httpx.Response,
    key_count: int = 1,
    key_index: int | None = None,
) -> None:
    state = load_state()
    provider_state = provider_state_entry(state, provider_name, create=True)
    provider_key_cooldowns(provider_state)
    provider_state["calls"] = parse_state_int(provider_state.get("calls")) + 1
    if key_index is not None and key_count > 0:
        provider_state["key_index"] = (key_index + 1) % key_count
    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining is not None:
        try:
            provider_state["last_remaining"] = int(remaining)
        except ValueError:
            provider_state["last_remaining"] = remaining
    save_state(state)


def append_request_log(entry: dict[str, Any]) -> None:
    state = load_state()
    record_request_stats(state, entry)
    requests = state.get("_requests")
    if not isinstance(requests, list):
        requests = []
        state["_requests"] = requests
    requests.insert(0, entry)
    del requests[REQUEST_LOG_LIMIT:]
    save_state(state)


def route_decision_entry(
    provider: dict[str, Any],
    fallback_count: int,
    key_index: int,
    key_count: int,
    previous_provider: str | None = None,
    previous_status: int | str | None = None,
    routing_candidate: RoutingCandidate | None = None,
) -> dict[str, Any]:
    provider_name = provider.get("name", "unknown")
    priority = safe_provider_priority(provider)
    attempt = fallback_count + 1
    if fallback_count <= 0 and routing_candidate and routing_candidate.reason != "primary":
        reason = routing_candidate.reason
        message = routing_candidate.message or f"智能路由优先尝试 {provider_name}。"
    elif fallback_count <= 0:
        reason = "primary"
        message = f"按质量优先顺序选择首个可用 provider：{provider_name}。"
    elif previous_provider == provider_name:
        reason = "key_retry"
        message = f"{provider_name} 的上一个 key 返回 {previous_status}，自动尝试同 provider 的下一个 key。"
    else:
        reason = "fallback"
        previous = previous_provider or "上一个 provider"
        message = f"{previous} 返回 {previous_status} 后，按优先级回退到 {provider_name}。"
    return {
        "reason": reason,
        "routing_reason": reason,
        "attempt": attempt,
        "priority": priority,
        "key_position": key_index + 1,
        "key_count": key_count,
        "message": message,
    }


def parse_state_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_state_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def request_log_entries(state: dict[str, Any], limit: int | None = None) -> list[dict[str, Any]]:
    requests = state.get("_requests")
    if not isinstance(requests, list):
        return []
    entries = [item for item in requests if isinstance(item, dict)]
    return entries[:limit] if limit is not None else entries


def provider_health(provider_name: str, provider_state: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    provider_stats_group = stats_container(state).get("providers")
    if not isinstance(provider_stats_group, dict):
        provider_stats_group = {}
    provider_stats = provider_stats_group.get(provider_name) or {}
    if not isinstance(provider_stats, dict):
        provider_stats = {}
    attempts = parse_state_int(provider_stats.get("attempts"))
    success = parse_state_int(provider_stats.get("success"))
    failed = parse_state_int(provider_stats.get("failed"))
    latency_total = parse_state_float(provider_stats.get("latency_ms_total"))
    now = time.time()
    cooldown_values = []
    for value in provider_key_cooldowns(provider_state).values():
        try:
            cooldown_until = float(value or 0)
        except (TypeError, ValueError):
            continue
        if cooldown_until > now:
            cooldown_values.append(cooldown_until)
    recent_request = next(
        (
            item for item in request_log_entries(state)
            if item.get("provider") == provider_name
        ),
        {},
    )

    if cooldown_values:
        status = "cooldown"
        label = "冷却中"
    elif attempts == 0:
        status = "unknown"
        label = "暂无请求"
    elif failed == 0:
        status = "healthy"
        label = "健康"
    elif success > 0:
        status = "degraded"
        label = "有波动"
    else:
        status = "failing"
        label = "失败"

    return {
        "status": status,
        "label": label,
        "attempts": attempts,
        "success": success,
        "failed": failed,
        "success_rate": round(success / attempts * 100, 1) if attempts else None,
        "avg_latency_ms": round(latency_total / attempts, 2) if attempts else 0,
        "recent_status": recent_request.get("status"),
        "recent_error": recent_request.get("error"),
        "recent_time": recent_request.get("time"),
        "cooldown_key_count": len(cooldown_values),
        "cooldown_seconds": max(0, int(max(cooldown_values) - now)) if cooldown_values else 0,
    }


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def write_config_file(config: dict[str, Any]) -> None:
    write_json_file(CONFIG_PATH, encrypt_config(config))


def safe_default_model(value: Any, default: str = "gpt-3.5-turbo") -> str:
    if not isinstance(value, str):
        return default
    return value.strip() or default


def load_raw_config() -> dict[str, Any]:
    default_config = {"providers": [], "default_model": "gpt-3.5-turbo"}
    try:
        raw_config = read_json_file(CONFIG_PATH, default_config)
    except (RuntimeError, UnicodeDecodeError) as exc:
        if not is_json_file_content_error(exc):
            raise
        raw_config = default_config

    config = decrypt_config(raw_config)
    config.setdefault("providers", [])
    config["default_model"] = safe_default_model(config.get("default_model"))
    config.setdefault("client_keys", [])
    return config


def load_config() -> dict[str, Any]:
    config = load_raw_config()
    providers = []
    for raw_provider in config.get("providers", []):
        provider = apply_env_overrides(raw_provider)
        name = safe_provider_name(provider)
        base_url = safe_provider_base_url(provider)
        if safe_bool(provider.get("enabled"), True) and name and base_url and provider_api_keys(provider):
            provider = dict(provider)
            provider["name"] = name
            provider["base_url"] = base_url
            provider["model"] = safe_provider_model(provider)
            providers.append(provider)
    providers = [provider_with_safe_priority(provider) for provider in providers]
    config["providers"] = sorted(providers, key=safe_provider_priority)
    return config


# ---------------------------------------------------------------------------
# Auth / rate-limit
# ---------------------------------------------------------------------------

def require_proxy_access(request: Request) -> None:
    require_proxy_access_token(request, PROXY_ACCESS_TOKEN)


def _request_token(request: Request) -> str:
    return request_token(request)


def _enabled_client_keys() -> list[dict[str, Any]]:
    return [
        client_key
        for client_key in client_key_entries(load_raw_config())
        if safe_bool(client_key.get("enabled"), True) and safe_secret_value(client_key.get("key", ""))
    ]


def enabled_client_key_count(config: dict[str, Any]) -> int:
    return sum(
        1
        for client_key in client_key_entries(config)
        if safe_bool(client_key.get("enabled"), True) and safe_secret_value(client_key.get("key", ""))
    )


def management_auth_mode() -> str:
    return auth_management_auth_mode(PROXY_ACCESS_TOKEN)


def v1_auth_mode(config: dict[str, Any]) -> str:
    return auth_v1_auth_mode(PROXY_ACCESS_TOKEN, enabled_client_key_count(config) > 0)


def _model_matches_any(model: str, patterns: list[str]) -> bool:
    return model_matches_any(model, patterns)


def _model_allowed_for_client_key(client_key: dict[str, Any], model: str | None) -> bool:
    if not model:
        return True
    allowed_models = client_key_model_rules(client_key, "allowed_models")
    excluded_models = client_key_model_rules(client_key, "excluded_models")
    return model_allowed_for_rules(model, allowed_models, excluded_models)


def _authorize_model_for_client_key(client_key: dict[str, Any], model: str | None) -> None:
    if not model:
        return
    allowed_models = client_key_model_rules(client_key, "allowed_models")
    excluded_models = client_key_model_rules(client_key, "excluded_models")
    authorize_model_for_rules(model, allowed_models, excluded_models)


def authorize_v1_access(request: Request, model: str | None = None) -> dict[str, Any] | None:
    token = _request_token(request)
    if secret_matches(token, PROXY_ACCESS_TOKEN):
        request.state.client_key_label = "proxy-token"
        return None

    client_keys = _enabled_client_keys()
    if not client_keys:
        if PROXY_ACCESS_TOKEN:
            raise HTTPException(status_code=401, detail="Missing or invalid local proxy access token")
        request.state.client_key_label = ""
        return None

    for client_key in client_keys:
        if secret_matches(token, safe_secret_value(client_key.get("key", ""))):
            request.state.client_key_label = client_key.get("label") or client_key.get("id") or "client-key"
            _authorize_model_for_client_key(client_key, model)
            return client_key
    raise HTTPException(status_code=401, detail="Missing or invalid local client API key")


def authorize_v1_request(request: Request, body: dict[str, Any]) -> dict[str, Any] | None:
    model = str(body.get("model", "")).strip() if isinstance(body, dict) else ""
    return authorize_v1_access(request, model or None)


def rate_limit_identity(request: Request) -> str:
    return request_rate_limit_identity(request)


def enforce_rate_limit(request: Request) -> None:
    enforce_request_rate_limit(request, RATE_LIMIT_BUCKETS, RATE_LIMIT_PER_MINUTE)


async def read_json_body(request: Request) -> Any:
    raw = await request.body()
    if MAX_REQUEST_BYTES > 0 and len(raw) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request body is too large")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc
    return body


async def read_v1_json_body(request: Request) -> dict[str, Any]:
    body = await read_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON request body must be an object")
    if "model" in body and not isinstance(body["model"], str):
        raise HTTPException(status_code=400, detail="model must be a string")
    authorize_v1_request(request, body)
    enforce_rate_limit(request)
    return body


# ---------------------------------------------------------------------------
# Proxy core logic
# ---------------------------------------------------------------------------

async def stream_response_bytes(
    response: httpx.Response,
    client: httpx.AsyncClient,
    log_entry: dict[str, Any] | None = None,
) -> AsyncIterator[bytes]:
    status = "stream_complete"
    error = None
    try:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
    except Exception as exc:
        status = "stream_error"
        error = str(exc)
        raise
    finally:
        if log_entry:
            log_entry["stream_status"] = status
            if error:
                log_entry["error"] = error
            append_request_log(log_entry)
        await response.aclose()


async def _try_provider(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    provider: dict[str, Any],
    default_model: str,
    api_key: str,
    stream: bool,
    fwd: dict[str, Any] | None = None,
) -> tuple[str, Any]:
    protocol = (fwd or {}).get("protocol", "openai")
    if protocol == "auto":
        protocol = provider_protocol(provider)
    passthrough = bool(fwd)
    path_suffix = (fwd or {}).get("path_suffix", "/chat/completions")

    if should_use_curl(provider):
        if stream:
            url, headers, request_body = _prepare_forward(
                body, provider, default_model, api_key, protocol, path_suffix, passthrough
            )
            return ("curl_stream", curl_stream_request(url, headers, request_body))
        try:
            status_code, data = await curl_forward_to_provider(
                body, provider, default_model, api_key,
                protocol=protocol, path_suffix=path_suffix, passthrough=passthrough,
            )
        except Exception:
            raise
        if status_code == 200:
            return ("curl", data)
        if status_code in RETRYABLE_STATUS_CODES:
            return ("retryable", {"status": status_code, "detail": json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)})
        raise HTTPException(status_code=status_code, detail=data)

    try:
        if stream:
            url, headers, request_body = _prepare_forward(
                body, provider, default_model, api_key, protocol, path_suffix, passthrough
            )
            upstream_request = client.build_request("POST", url, headers=headers, json=request_body)
            response = await client.send(upstream_request, stream=True)
        else:
            response = await forward_to_provider(
                client, body, provider, default_model, api_key,
                protocol=protocol, path_suffix=path_suffix, passthrough=passthrough,
            )
    except httpx.RequestError:
        raise

    if response.status_code == 200:
        return ("response", response)
    if stream:
        error_bytes = await response.aread()
        await response.aclose()
        error_text = error_bytes.decode("utf-8", errors="replace")
    else:
        error_text = response.text
    if response.status_code in RETRYABLE_STATUS_CODES:
        return ("retryable", {"status": response.status_code, "detail": error_text})
    if stream:
        raise HTTPException(status_code=response.status_code, detail=error_text)
    return ("fatal_response", JSONResponse(content=safe_response_detail(response), status_code=response.status_code))


def _request_model_for_log(body: dict[str, Any], path: str, default_model: str) -> str:
    if isinstance(body, dict) and str(body.get("model", "")).strip():
        return str(body["model"]).strip()
    marker = "/v1beta/models/"
    if path.startswith(marker) and ":" in path:
        return path[len(marker):].split(":", 1)[0]
    return default_model


async def _iterate_providers(
    body: dict[str, Any],
    config: dict[str, Any],
    client: httpx.AsyncClient,
    callback: Any,
    stream: bool,
    path: str = "/v1/chat/completions",
    fwd: dict[str, Any] | None = None,
    log_model: str | None = None,
    client_key_label: str | None = None,
) -> Any:
    last_error = "所有后端 API 均不可用"
    fallback_count = 0
    state = load_state()
    log_model = log_model or _request_model_for_log(body, path, config.get("default_model", "gpt-3.5-turbo"))
    previous_provider: str | None = None
    previous_status: int | str | None = None

    for routing_candidate in order_providers_for_request(config["providers"], state):
        provider = routing_candidate.provider
        provider_name = provider["name"]
        key_attempts = key_attempt_order(provider, state)
        key_count = len(provider_api_keys(provider))
        for key_index, api_key in key_attempts:
            started_at = time.perf_counter()
            route_decision = route_decision_entry(
                provider,
                fallback_count,
                key_index,
                key_count,
                previous_provider=previous_provider,
                previous_status=previous_status,
                routing_candidate=routing_candidate,
            )
            try:
                kind, data = await _try_provider(client, body, provider, config["default_model"], api_key, stream, fwd)
            except httpx.RequestError as exc:
                logger.info("provider=%s status=request_error path=%s", provider_name, path)
                last_error = str(exc)
                append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error, streamed=stream, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision))
                previous_provider = provider_name
                previous_status = "request_error"
                fallback_count += 1
                continue
            except HTTPException as exc:
                logger.info("provider=%s status=%s path=%s", provider_name, exc.status_code, path)
                append_request_log(request_log_entry(provider_name, exc.status_code, started_at, fallback_count, streamed=stream, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision))
                raise

            if kind == "retryable":
                retry_info = data
                logger.info("provider=%s status=%s path=%s", provider_name, retry_info["status"], path)
                append_request_log(request_log_entry(provider_name, retry_info["status"], started_at, fallback_count, retry_info["detail"], streamed=stream, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision))
                if retry_info["status"] == 429:
                    record_key_cooldown(provider_name, api_key)
                last_error = retry_info["detail"]
                previous_provider = provider_name
                previous_status = retry_info["status"]
                fallback_count += 1
                continue

            if kind == "fatal_response":
                return data

            logger.info("provider=%s status=200 path=%s", provider_name, path)
            if kind == "response":
                record_success(provider_name, data, key_count, key_index)
            else:
                record_success(provider_name, httpx.Response(200, json={}), key_count, key_index)
            return callback(data, provider_name, started_at, fallback_count, key_attempts, key_index, client, stream, path, log_model, client_key_label, route_decision)

    raise HTTPException(status_code=502, detail=last_error)


def _stream_callback(
    data: Any,
    provider_name: str,
    started_at: float,
    fallback_count: int,
    key_attempts: list,
    key_index: int,
    client: httpx.AsyncClient,
    stream: bool,
    path: str,
    log_model: str | None,
    client_key_label: str | None,
    route_decision: dict[str, Any] | None,
) -> StreamingResponse:
    if isinstance(data, httpx.Response):
        log_entry = request_log_entry(provider_name, data.status_code, started_at, fallback_count, streamed=True, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision)
        return StreamingResponse(
            stream_response_bytes(data, client, log_entry),
            media_type=data.headers.get("content-type", "text/event-stream"),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _curl_stream_gen(async_iter, log_entry: dict[str, Any]):
        try:
            async for chunk in async_iter:
                yield chunk
        except Exception as exc:
            log_entry["stream_status"] = "stream_error"
            log_entry["error"] = str(exc)
            raise
        else:
            log_entry["stream_status"] = "stream_complete"
        finally:
            append_request_log(log_entry)

    if hasattr(data, "__aiter__"):
        log_entry = request_log_entry(provider_name, 200, started_at, fallback_count, streamed=True, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision)
        return StreamingResponse(_curl_stream_gen(data, log_entry), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    log_entry = request_log_entry(provider_name, 200, started_at, fallback_count, streamed=True, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision)
    log_entry["stream_status"] = "stream_complete"
    append_request_log(log_entry)
    payload = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
    return StreamingResponse(iter([payload.encode("utf-8")]), media_type="text/event-stream")


def _json_callback(
    data: Any,
    provider_name: str,
    started_at: float,
    fallback_count: int,
    key_attempts: list,
    key_index: int,
    client: httpx.AsyncClient,
    stream: bool,
    path: str,
    log_model: str | None,
    client_key_label: str | None,
    route_decision: dict[str, Any] | None,
) -> JSONResponse:
    append_request_log(request_log_entry(provider_name, 200, started_at, fallback_count, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision))
    content = data.json() if isinstance(data, httpx.Response) else data
    return JSONResponse(content=content, status_code=200)


async def stream_chat_completions(
    body: dict[str, Any],
    config: dict[str, Any],
    request: Request | None = None,
    path: str = "/v1/chat/completions",
    fwd: dict[str, Any] | None = None,
) -> StreamingResponse:
    client = get_http_client(request.app) if request else create_http_client()
    client_key_label = getattr(request.state, "client_key_label", None) if request else None
    return await _iterate_providers(
        body, config, client, _stream_callback,
        stream=True, path=path, fwd=fwd,
        log_model=_request_model_for_log(body, path, config.get("default_model", "gpt-3.5-turbo")),
        client_key_label=client_key_label,
    )


async def proxy_chat_json(
    body: dict[str, Any],
    config: dict[str, Any],
    request: Request | None = None,
    path: str = "/v1/chat/completions",
    fwd: dict[str, Any] | None = None,
) -> JSONResponse:
    client = get_http_client(request.app) if request else create_http_client()
    client_key_label = getattr(request.state, "client_key_label", None) if request else None
    return await _iterate_providers(
        body, config, client, _json_callback,
        stream=False, path=path, fwd=fwd,
        log_model=_request_model_for_log(body, path, config.get("default_model", "gpt-3.5-turbo")),
        client_key_label=client_key_label,
    )


# ---------------------------------------------------------------------------
# Protocol routing helpers
# ---------------------------------------------------------------------------

def _filter_config_by_protocol(config: dict[str, Any], protocols: set[str]) -> dict[str, Any]:
    """Return a shallow copy of config keeping only providers whose protocol is in `protocols`."""
    providers = [p for p in config["providers"] if provider_protocol(p) in protocols]
    return {"providers": providers, "default_model": config.get("default_model", "gpt-3.5-turbo")}


async def _passthrough(
    body: dict[str, Any],
    config: dict[str, Any],
    request: Request,
    protocol: str,
    path_suffix: str,
    inbound_path: str,
) -> Any:
    """Forward the client body verbatim to the matching backend protocol."""
    fwd = {"protocol": protocol, "path_suffix": path_suffix}
    if body.get("stream") is True:
        return await stream_chat_completions(body, config, request=request, path=inbound_path, fwd=fwd)
    return await proxy_chat_json(body, config, request=request, path=inbound_path, fwd=fwd)


# ---------------------------------------------------------------------------
# App & routes
# ---------------------------------------------------------------------------

app = FastAPI(title="Local GPT API Proxy", lifespan=lifespan)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_dashboard_assets(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
    return response


@app.get("/")
def dashboard() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard files are missing")
    return FileResponse(index_path, headers={"Cache-Control": "no-store, max-age=0"})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions endpoint.
    Supports both OpenAI and domestic OpenAI-compatible model providers.
    """
    body = await read_v1_json_body(request)
    config = _filter_config_by_protocol(load_config(), {"openai", "domestic"})
    if not config["providers"]:
        raise HTTPException(
            status_code=503,
            detail="No OpenAI or domestic model providers configured. Please add at least one provider with protocol 'openai' or 'domestic'."
        )
    return await _passthrough(body, config, request, "auto", "/chat/completions", "/v1/chat/completions")


@app.post("/v1/responses")
async def responses(request: Request):
    """
    OpenAI responses endpoint.
    Only routes to providers with protocol 'openai'.
    """
    body = await read_v1_json_body(request)
    config = _filter_config_by_protocol(load_config(), {"openai"})
    if not config["providers"]:
        raise HTTPException(
            status_code=503,
            detail="No OpenAI providers configured. Please add at least one provider with protocol 'openai'."
        )
    return await _passthrough(body, config, request, "openai", "/responses", "/v1/responses")


@app.post("/v1/messages")
async def messages(request: Request):
    """
    Claude Messages API endpoint.
    Only routes to providers with protocol 'claude'.
    """
    body = await read_v1_json_body(request)
    config = _filter_config_by_protocol(load_config(), {"claude"})
    if not config["providers"]:
        raise HTTPException(
            status_code=503,
            detail="No Claude providers configured. Please add at least one provider with protocol 'claude' and base_url 'https://api.anthropic.com/v1'."
        )
    return await _passthrough(body, config, request, "claude", "/messages", "/v1/messages")


@app.post("/v1beta/models/{rest:path}")
async def gemini_generate(rest: str, request: Request):
    """
    Google Gemini API endpoint.
    Supports both generateContent and streamGenerateContent.
    Only routes to providers with protocol 'gemini'.
    """
    body = await read_v1_json_body(request)
    config = _filter_config_by_protocol(load_config(), {"gemini"})
    if not config["providers"]:
        raise HTTPException(
            status_code=503,
            detail="No Gemini providers configured. Please add at least one provider with protocol 'gemini' and base_url 'https://generativelanguage.googleapis.com/v1beta'."
        )
    # rest looks like "gemini-pro:generateContent" or "gemini-pro:streamGenerateContent"
    if ":" not in rest:
        raise HTTPException(status_code=404, detail="Invalid Gemini endpoint. Expected format: /v1beta/models/{model}:generateContent")
    model, verb = rest.rsplit(":", 1)
    if verb not in {"generateContent", "streamGenerateContent"}:
        raise HTTPException(status_code=404, detail=f"Unsupported Gemini verb: {verb}. Supported verbs: generateContent, streamGenerateContent")
    # streamGenerateContent (or ?alt=sse) implies streaming
    if verb == "streamGenerateContent" or request.query_params.get("alt") == "sse":
        body["stream"] = True
    path_suffix = f"/models/{model}:{verb}"
    return await _passthrough(body, config, request, "gemini", path_suffix, f"/v1beta/models/{rest}")


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


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    client_key = authorize_v1_access(request)
    enforce_rate_limit(request)
    config = load_config()
    seen = set()
    models = []
    for provider in config["providers"]:
        try:
            provider_models = await fetch_provider_models(provider)
        except Exception as exc:
            logger.info("provider=%s status=model_fetch_error detail=%s", provider.get("name", "unknown"), exc)
            provider_models = []
        provider_models = normalize_model_entries(provider_models)
        if not provider_models and provider.get("model"):
            provider_models = normalize_model_entries([{"id": provider["model"], "object": "model"}])
        for model in provider_models:
            model_id = model["id"]
            if client_key and not _model_allowed_for_client_key(client_key, model_id):
                continue
            if model_id in seen:
                continue
            seen.add(model_id)
            item = dict(model)
            item.setdefault("object", "model")
            item["owned_by"] = item.get("owned_by") or provider["name"]
            models.append(item)
    return {"object": "list", "data": models}


@app.get("/health")
def health() -> dict[str, Any]:
    """Basic health check endpoint."""
    return {"status": "ok"}


@app.get("/health/detailed")
def health_detailed() -> dict[str, Any]:
    """
    Detailed health check showing protocol availability.
    Does not require authentication.
    """
    config = load_config()
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
            "gemini_generate": "/v1beta/models/{model}:generateContent (Gemini only)"
        }
    }


@app.get("/api/protocols")
def protocols_endpoint(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    config = load_config()
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
    require_proxy_access(request)
    return {"presets": provider_presets()}


@app.get("/api/config")
def get_config(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    config = load_raw_config()
    state = load_state()
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
            "proxy_access_token_enabled": bool(PROXY_ACCESS_TOKEN),
            "management_auth_mode": management_auth_mode(),
            "v1_auth_mode": v1_auth_mode(config),
            "enabled_client_key_count": enabled_client_key_count(config),
            "config_encryption_enabled": bool(CONFIG_ENCRYPTION_SECRET),
            "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
            "max_request_bytes": MAX_REQUEST_BYTES,
            "key_cooldown_seconds": KEY_COOLDOWN_SECONDS,
        },
    }


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


@app.post("/api/config")
async def save_config(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    try:
        payload = await read_json_body(request)
        config = normalize_config_payload(payload, load_raw_config())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_config_file(config)
    return get_config(request)


@app.delete("/api/providers/{provider_name}")
def delete_provider(provider_name: str, request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    config = load_raw_config()
    providers = config.get("providers", [])
    remaining_providers = [
        provider for provider in providers if provider.get("name") != provider_name
    ]
    if len(remaining_providers) == len(providers):
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")

    config["providers"] = remaining_providers
    write_config_file(config)

    state = load_state()
    if provider_name in state:
        state.pop(provider_name, None)
        save_state(state)

    return get_config(request)


@app.get("/api/config/export")
def export_config(request: Request, redacted: bool = False) -> dict[str, Any]:
    require_proxy_access(request)
    config = load_raw_config()
    if redacted:
        return redacted_config_export(config)
    return config


@app.post("/api/config/import")
async def import_config(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    try:
        payload = await read_json_body(request)
        if isinstance(payload, dict) and payload.get("redacted") is True:
            raise ValueError("Redacted config exports cannot be imported because secrets are omitted")
        config = normalize_config_payload(payload, {"providers": []})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    write_config_file(config)
    return get_config(request)


@app.get("/api/providers")
def provider_status(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    config = load_config()
    state = load_state()
    providers = []
    for provider in config["providers"]:
        provider_state = provider_state_entry(state, provider["name"])
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
                "health": provider_health(provider["name"], provider_state, state),
            }
        )
    return {"providers": providers}


@app.get("/api/requests")
def recent_requests(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    state = load_state()
    return {"requests": request_log_entries(state, REQUEST_LOG_LIMIT)}


@app.get("/api/stats")
def request_stats(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    return summarize_request_stats(load_state())


@app.delete("/api/observability")
def clear_observability(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    state = load_state()
    requests = state.get("_requests")
    cleared_requests = len(requests) if isinstance(requests, list) else 0
    cleared_stats = "_stats" in state
    state["_requests"] = []
    state.pop("_stats", None)
    save_state(state)
    return {
        "cleared": {
            "requests": cleared_requests,
            "stats": cleared_stats,
        },
        "requests": [],
        "stats": summarize_request_stats(state),
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
        return "disabled", "已停用，不会参与本次路由预览。"
    if not provider.get("name"):
        return "missing_name", "缺少 provider 名称，无法参与路由。"
    if not provider.get("base_url"):
        return "missing_base_url", "缺少 Base URL，无法参与路由。"
    if not provider_api_keys(provider):
        return "missing_key", "缺少 API Key，无法参与路由。"
    if protocol not in protocols:
        return "protocol_mismatch", f"协议 {protocol} 不属于本次预览目标。"
    return None


def routing_preview_skipped_providers(raw_providers: list[dict[str, Any]], protocols: set[str]) -> list[dict[str, Any]]:
    skipped: list[dict[str, Any]] = []
    for provider in sorted(raw_providers, key=safe_provider_priority):
        provider_with_env = apply_env_overrides(provider)
        provider_with_env = provider_with_safe_priority(provider_with_env)
        skip = routing_preview_skip_reason(provider_with_env, protocols)
        if skip is None:
            continue
        reason, message = skip
        skipped.append(
            {
                "name": provider_with_env.get("name") or "未命名 provider",
                "protocol": provider_protocol(provider_with_env),
                "priority": safe_provider_priority(provider_with_env),
                "reason": reason,
                "message": message,
            }
        )
    return skipped


@app.get("/api/routing/preview")
def routing_preview(request: Request, target: str = "chat") -> dict[str, Any]:
    require_proxy_access(request)
    target_key = (target or "chat").strip().lower()
    protocols = ROUTING_PREVIEW_TARGETS.get(target_key)
    if protocols is None:
        allowed = ", ".join(sorted(ROUTING_PREVIEW_TARGETS))
        raise HTTPException(status_code=400, detail=f"Unknown routing preview target '{target}'. Expected one of: {allowed}")

    raw_config = load_raw_config()
    config = _filter_config_by_protocol(load_config(), protocols)
    state = load_state()
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
    require_proxy_access(request)
    config = load_config()
    semaphore = asyncio.Semaphore(6)

    async def provider_coverage(provider: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                models = await fetch_provider_models(provider)
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
                "detail": detail if model_ids or not fallback_used else "使用配置中的默认模型",
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
    require_proxy_access(request)
    config = load_config()
    raw_config = load_raw_config()
    raw_names = {provider.get("name") for provider in raw_config.get("providers", []) if provider.get("name")}
    for provider in config["providers"]:
        if provider["name"] == provider_name:
            try:
                result = await check_provider(provider, config["default_model"])
            except Exception as exc:
                logger.info("provider=%s status=check_error detail=%s", provider_name, exc)
                result = {"ok": False, "status": "check_error", "detail": str(exc)}
            return {"provider": provider_name, **result}
    if provider_name in raw_names:
        return {
            "provider": provider_name,
            "ok": False,
            "status": "no_api_key",
            "detail": "该 API 尚未启用或尚未填写密钥，请先在 UI 中启用并输入 API Key 后保存",
        }
    raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' does not exist in config")


@app.post("/api/providers/check-all")
async def providers_check_all(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    config = load_config()
    semaphore = asyncio.Semaphore(6)

    async def run_check(provider: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                result = await check_provider(provider, config["default_model"])
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
    require_proxy_access(request)
    config = load_config()
    raw_config = load_raw_config()
    raw_names = {provider.get("name") for provider in raw_config.get("providers", []) if provider.get("name")}
    for provider in config["providers"]:
        if provider["name"] == provider_name:
            try:
                models = normalize_model_entries(await fetch_provider_models(provider))
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
            "detail": "该 API 尚未启用或尚未填写密钥，请先在 UI 中启用并输入 API Key 后保存",
            "models": {"object": "list", "data": []},
        }
    raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")


@app.post("/api/providers/models/sync")
async def provider_models_sync(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    config = load_config()
    semaphore = asyncio.Semaphore(6)

    async def run_fetch(provider: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            try:
                models = normalize_model_entries(await fetch_provider_models(provider))
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
                "detail": "ok" if models else "未返回模型列表",
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
