import asyncio
import fnmatch
import hmac
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

from ._config import (
    apply_env_overrides,
    decrypt_config,
    editable_client_key,
    editable_provider,
    encrypt_config,
    normalize_config_payload,
    provider_api_keys,
    read_json_file,
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
from ._state import key_fingerprint, record_request_stats, request_log_entry, summarize_request_stats

# ---------------------------------------------------------------------------
# Monkeypatchable module-level constants
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = Path(os.getenv("GPT_PROXY_CONFIG", BASE_DIR / "config.json"))
STATE_PATH = Path(os.getenv("GPT_PROXY_STATE", BASE_DIR / "state.json"))
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
REQUEST_LOG_LIMIT = 50
PROXY_ACCESS_TOKEN = os.getenv("GPT_PROXY_ACCESS_TOKEN", "").strip()
CONFIG_ENCRYPTION_SECRET = os.getenv("GPT_PROXY_CONFIG_SECRET", "").strip()
RATE_LIMIT_PER_MINUTE = int(os.getenv("GPT_PROXY_RATE_LIMIT_PER_MINUTE", "0") or 0)
MAX_REQUEST_BYTES = int(os.getenv("GPT_PROXY_MAX_REQUEST_BYTES", str(2 * 1024 * 1024)) or 0)
KEY_COOLDOWN_SECONDS = int(os.getenv("GPT_PROXY_KEY_COOLDOWN_SECONDS", "60") or 0)
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
        _state_cache = read_json_file(STATE_PATH, {})
        _state_cache_path = STATE_PATH
    return _state_cache


def save_state(state: dict[str, Any]) -> None:
    global _state_cache, _state_cache_path
    _state_cache = state
    _state_cache_path = STATE_PATH
    with _state_write_lock:
        write_json_file(STATE_PATH, state)


def record_key_cooldown(provider_name: str, api_key: str) -> None:
    if KEY_COOLDOWN_SECONDS <= 0:
        return
    state = load_state()
    provider_state = state.setdefault(provider_name, {"calls": 0, "last_remaining": None})
    cooldowns = provider_state.setdefault("key_cooldowns", {})
    cooldowns[key_fingerprint(api_key)] = time.time() + KEY_COOLDOWN_SECONDS
    save_state(state)


def key_attempt_order(provider: dict[str, Any], state: dict[str, Any]) -> list[tuple[int, str]]:
    keys = provider_api_keys(provider)
    if not keys:
        return []
    provider_state = state.get(provider["name"], {})
    start_index = int(provider_state.get("key_index", 0)) % len(keys)
    ordered_indexes = list(range(start_index, len(keys))) + list(range(0, start_index))
    cooldowns = provider_state.get("key_cooldowns", {})
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
    provider_state = state.setdefault(provider_name, {"calls": 0, "last_remaining": None})
    provider_state["calls"] = int(provider_state.get("calls", 0)) + 1
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
    requests = state.setdefault("_requests", [])
    requests.insert(0, entry)
    del requests[REQUEST_LOG_LIMIT:]
    save_state(state)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def write_config_file(config: dict[str, Any]) -> None:
    write_json_file(CONFIG_PATH, encrypt_config(config))


def load_raw_config() -> dict[str, Any]:
    config = decrypt_config(read_json_file(CONFIG_PATH, {"providers": [], "default_model": "gpt-3.5-turbo"}))
    config.setdefault("providers", [])
    config.setdefault("default_model", "gpt-3.5-turbo")
    config.setdefault("client_keys", [])
    return config


def load_config() -> dict[str, Any]:
    config = load_raw_config()
    providers = [apply_env_overrides(provider) for provider in config.get("providers", [])]
    providers = [
        provider
        for provider in providers
        if provider.get("enabled", True)
        and provider.get("name")
        and provider.get("base_url")
        and provider_api_keys(provider)
    ]
    config["providers"] = sorted(providers, key=lambda item: item.get("priority", 1000))
    return config


# ---------------------------------------------------------------------------
# Auth / rate-limit
# ---------------------------------------------------------------------------

def require_proxy_access(request: Request) -> None:
    if not PROXY_ACCESS_TOKEN:
        return
    auth_header = request.headers.get("authorization", "")
    x_api_key = request.headers.get("x-api-key", "")
    if auth_header == f"Bearer {PROXY_ACCESS_TOKEN}" or x_api_key == PROXY_ACCESS_TOKEN:
        return
    raise HTTPException(status_code=401, detail="Missing or invalid local proxy access token")


def _request_token(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    x_api_key = request.headers.get("x-api-key", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return x_api_key.strip()


def _enabled_client_keys() -> list[dict[str, Any]]:
    return [
        client_key
        for client_key in load_raw_config().get("client_keys", [])
        if client_key.get("enabled", True) and str(client_key.get("key", "")).strip()
    ]


def _model_matches_any(model: str, patterns: list[str]) -> bool:
    normalized = model.strip().lower()
    return any(fnmatch.fnmatchcase(normalized, pattern.strip().lower()) for pattern in patterns if pattern.strip())


def _authorize_model_for_client_key(client_key: dict[str, Any], model: str | None) -> None:
    if not model:
        return
    allowed_models = [str(item) for item in client_key.get("allowed_models", [])]
    excluded_models = [str(item) for item in client_key.get("excluded_models", [])]
    if allowed_models and not _model_matches_any(model, allowed_models):
        raise HTTPException(status_code=403, detail=f"Model '{model}' is not allowed for this local client key")
    if excluded_models and _model_matches_any(model, excluded_models):
        raise HTTPException(status_code=403, detail=f"Model '{model}' is excluded for this local client key")


def authorize_v1_request(request: Request, body: dict[str, Any]) -> None:
    token = _request_token(request)
    if PROXY_ACCESS_TOKEN and hmac.compare_digest(token, PROXY_ACCESS_TOKEN):
        request.state.client_key_label = "proxy-token"
        return

    client_keys = _enabled_client_keys()
    if not client_keys:
        if PROXY_ACCESS_TOKEN:
            raise HTTPException(status_code=401, detail="Missing or invalid local proxy access token")
        request.state.client_key_label = ""
        return

    for client_key in client_keys:
        if hmac.compare_digest(token, str(client_key.get("key", ""))):
            request.state.client_key_label = client_key.get("label") or client_key.get("id") or "client-key"
            model = str(body.get("model", "")).strip() if isinstance(body, dict) else ""
            _authorize_model_for_client_key(client_key, model or None)
            return
    raise HTTPException(status_code=401, detail="Missing or invalid local client API key")


def enforce_rate_limit(request: Request) -> None:
    if RATE_LIMIT_PER_MINUTE <= 0:
        return
    client_host = request.client.host if request.client else "local"
    identity = request.headers.get("authorization") or request.headers.get("x-api-key") or client_host
    now = time.time()
    window_start = now - 60

    stale_threshold = now - 120
    stale_keys = [k for k, v in RATE_LIMIT_BUCKETS.items() if not v or v[-1] < stale_threshold]
    for k in stale_keys:
        del RATE_LIMIT_BUCKETS[k]

    bucket = [timestamp for timestamp in RATE_LIMIT_BUCKETS.get(identity, []) if timestamp > window_start]
    if len(bucket) >= RATE_LIMIT_PER_MINUTE:
        RATE_LIMIT_BUCKETS[identity] = bucket
        raise HTTPException(status_code=429, detail="Local proxy rate limit exceeded")
    bucket.append(now)
    RATE_LIMIT_BUCKETS[identity] = bucket


async def read_v1_json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if MAX_REQUEST_BYTES > 0 and len(raw) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request body is too large")
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc
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

    for provider in config["providers"]:
        provider_name = provider["name"]
        key_attempts = key_attempt_order(provider, state)
        key_count = len(provider_api_keys(provider))
        for key_index, api_key in key_attempts:
            started_at = time.perf_counter()
            try:
                kind, data = await _try_provider(client, body, provider, config["default_model"], api_key, stream, fwd)
            except httpx.RequestError as exc:
                logger.info("provider=%s status=request_error path=%s", provider_name, path)
                last_error = str(exc)
                append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error, streamed=stream, path=path, model=log_model, client_key=client_key_label))
                fallback_count += 1
                continue
            except HTTPException as exc:
                logger.info("provider=%s status=%s path=%s", provider_name, exc.status_code, path)
                append_request_log(request_log_entry(provider_name, exc.status_code, started_at, fallback_count, streamed=stream, path=path, model=log_model, client_key=client_key_label))
                raise

            if kind == "retryable":
                retry_info = data
                logger.info("provider=%s status=%s path=%s", provider_name, retry_info["status"], path)
                append_request_log(request_log_entry(provider_name, retry_info["status"], started_at, fallback_count, retry_info["detail"], streamed=stream, path=path, model=log_model, client_key=client_key_label))
                if retry_info["status"] == 429:
                    record_key_cooldown(provider_name, api_key)
                last_error = retry_info["detail"]
                fallback_count += 1
                continue

            if kind == "fatal_response":
                return data

            logger.info("provider=%s status=200 path=%s", provider_name, path)
            if kind == "response":
                record_success(provider_name, data, key_count, key_index)
            else:
                record_success(provider_name, httpx.Response(200, json={}), key_count, key_index)
            return callback(data, provider_name, started_at, fallback_count, key_attempts, key_index, client, stream, path, log_model, client_key_label)

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
) -> StreamingResponse:
    if isinstance(data, httpx.Response):
        log_entry = request_log_entry(provider_name, data.status_code, started_at, fallback_count, streamed=True, path=path, model=log_model, client_key=client_key_label)
        return StreamingResponse(
            stream_response_bytes(data, client, log_entry),
            media_type=data.headers.get("content-type", "text/event-stream"),
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _curl_stream_gen(async_iter):
        async for chunk in async_iter:
            yield chunk

    if hasattr(data, "__aiter__"):
        log_entry = request_log_entry(provider_name, 200, started_at, fallback_count, streamed=True, path=path, model=log_model, client_key=client_key_label)
        log_entry["stream_status"] = "stream_complete"
        append_request_log(log_entry)
        return StreamingResponse(_curl_stream_gen(data), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    log_entry = request_log_entry(provider_name, 200, started_at, fallback_count, streamed=True, path=path, model=log_model, client_key=client_key_label)
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
) -> JSONResponse:
    append_request_log(request_log_entry(provider_name, 200, started_at, fallback_count, path=path, model=log_model, client_key=client_key_label))
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


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    enforce_rate_limit(request)
    config = load_config()
    seen = set()
    models = []
    for provider in config["providers"]:
        provider_models = await fetch_provider_models(provider)
        if not provider_models and provider.get("model"):
            provider_models = [{"id": provider["model"], "object": "model"}]
        for model in provider_models:
            model_id = model["id"]
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
            editable_provider(provider, state, config.get("default_model", "gpt-3.5-turbo"))
            for provider in sorted(config.get("providers", []), key=lambda item: item.get("priority", 1000))
        ],
        "client_keys": [
            editable_client_key(client_key)
            for client_key in config.get("client_keys", [])
        ],
        "security": {
            "proxy_access_token_enabled": bool(PROXY_ACCESS_TOKEN),
            "config_encryption_enabled": bool(CONFIG_ENCRYPTION_SECRET),
            "rate_limit_per_minute": RATE_LIMIT_PER_MINUTE,
            "max_request_bytes": MAX_REQUEST_BYTES,
            "key_cooldown_seconds": KEY_COOLDOWN_SECONDS,
        },
    }


@app.post("/api/config")
async def save_config(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    try:
        payload = await request.json()
        config = normalize_config_payload(payload, load_raw_config())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc
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
def export_config(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    return load_raw_config()


@app.post("/api/config/import")
async def import_config(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    try:
        payload = await request.json()
        config = normalize_config_payload(payload, {"providers": []})
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc
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
        providers.append(
            {
                "name": provider["name"],
                "protocol": provider_protocol(provider),
                "base_url": provider["base_url"],
                "model": provider.get("model", config["default_model"]),
                "priority": provider.get("priority", 1000),
                "enabled": provider.get("enabled", True),
                "key_count": len(provider_api_keys(provider)),
                "calls": state.get(provider["name"], {}).get("calls", 0),
                "last_remaining": state.get(provider["name"], {}).get("last_remaining"),
            }
        )
    return {"providers": providers}


@app.get("/api/requests")
def recent_requests(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    state = load_state()
    return {"requests": state.get("_requests", [])[:REQUEST_LOG_LIMIT]}


@app.get("/api/stats")
def request_stats(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    return summarize_request_stats(load_state())


@app.post("/api/providers/{provider_name}/check")
async def provider_check(provider_name: str, request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    config = load_config()
    raw_config = load_raw_config()
    raw_names = {provider.get("name") for provider in raw_config.get("providers", []) if provider.get("name")}
    for provider in config["providers"]:
        if provider["name"] == provider_name:
            result = await check_provider(provider, config["default_model"])
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
            result = await check_provider(provider, config["default_model"])
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
            models = await fetch_provider_models(provider)
            return {"provider": provider_name, "status": 200, "models": {"object": "list", "data": models}}
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
                models = await fetch_provider_models(provider)
            except Exception as exc:
                return {
                    "provider": provider["name"],
                    "protocol": provider_protocol(provider),
                    "model": provider.get("model", config["default_model"]),
                    "ok": False,
                    "count": 0,
                    "models": [],
                    "detail": str(exc),
                }

            fallback_used = False
            if not models and provider.get("model"):
                fallback_used = True
                models = [{"id": provider["model"], "object": "model"}]

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
