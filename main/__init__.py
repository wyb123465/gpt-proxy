import asyncio
import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .auth import (
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
    encrypt_config,
    provider_api_keys,
    provider_with_safe_priority,
    read_json_file,
    safe_bool,
    safe_provider_model,
    safe_provider_base_url,
    safe_provider_name,
    safe_provider_priority,
    safe_secret_value,
    write_json_file,
)
from ._providers import check_provider, curl_forward_to_provider, fetch_provider_models
from ._routing import RoutingCandidate
from ._state import key_fingerprint, record_request_stats, request_log_entry, stats_container, summarize_request_stats
from .proxy import ProxyServices
from .routes_admin import AdminRouteServices, register_admin_routes
from .routes_v1 import V1RouteServices, register_v1_routes

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
# Proxy core wiring
# ---------------------------------------------------------------------------

def proxy_services() -> ProxyServices:
    return ProxyServices(
        create_http_client=create_http_client,
        get_http_client=get_http_client,
        load_state=load_state,
        append_request_log=append_request_log,
        key_attempt_order=key_attempt_order,
        record_key_cooldown=record_key_cooldown,
        record_success=record_success,
        route_decision_entry=route_decision_entry,
    )


async def current_check_provider(provider: dict[str, Any], default_model: str) -> dict[str, Any]:
    return await check_provider(provider, default_model)


async def current_fetch_provider_models(provider: dict[str, Any]) -> list[dict[str, Any]]:
    return await fetch_provider_models(provider)

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


def v1_route_services() -> V1RouteServices:
    return V1RouteServices(
        read_v1_json_body=read_v1_json_body,
        load_config=load_config,
        authorize_v1_access=authorize_v1_access,
        enforce_rate_limit=enforce_rate_limit,
        model_allowed_for_client_key=_model_allowed_for_client_key,
        fetch_provider_models=current_fetch_provider_models,
        proxy_services=proxy_services,
    )


def admin_route_services() -> AdminRouteServices:
    return AdminRouteServices(
        require_proxy_access=require_proxy_access,
        read_json_body=read_json_body,
        load_config=load_config,
        load_raw_config=load_raw_config,
        load_state=load_state,
        save_state=save_state,
        write_config_file=write_config_file,
        check_provider=current_check_provider,
        fetch_provider_models=current_fetch_provider_models,
        enabled_client_key_count=enabled_client_key_count,
        management_auth_mode=management_auth_mode,
        v1_auth_mode=v1_auth_mode,
        provider_state_entry=provider_state_entry,
        provider_health=provider_health,
        request_log_entries=request_log_entries,
        proxy_access_token_enabled=lambda: bool(PROXY_ACCESS_TOKEN),
        config_encryption_enabled=lambda: bool(CONFIG_ENCRYPTION_SECRET),
        rate_limit_per_minute=lambda: RATE_LIMIT_PER_MINUTE,
        max_request_bytes=lambda: MAX_REQUEST_BYTES,
        key_cooldown_seconds=lambda: KEY_COOLDOWN_SECONDS,
        request_log_limit=REQUEST_LOG_LIMIT,
    )


register_v1_routes(app, v1_route_services())
register_admin_routes(app, admin_route_services())
