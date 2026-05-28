import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
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

app = FastAPI(title="Local GPT API Proxy")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="[%(asctime)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("gpt_proxy")


def create_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30.0)


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists() or path.stat().st_size == 0:
        return default
    try:
        with path.open("r", encoding="utf-8-sig") as file:
            return json.load(file)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON file: {path}") from exc


def write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    with temp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
    temp_path.replace(path)


def encryption_fernet() -> Fernet | None:
    if not CONFIG_ENCRYPTION_SECRET:
        return None
    key = base64.urlsafe_b64encode(hashlib.sha256(CONFIG_ENCRYPTION_SECRET.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret_value(value: str) -> str:
    fernet = encryption_fernet()
    if not fernet or value.startswith("enc:"):
        return value
    return "enc:" + fernet.encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret_value(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("enc:"):
        return value
    fernet = encryption_fernet()
    if not fernet:
        raise RuntimeError("Encrypted config.json API key requires GPT_PROXY_CONFIG_SECRET")
    try:
        return fernet.decrypt(value[4:].encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        raise RuntimeError("Unable to decrypt config.json API key; check GPT_PROXY_CONFIG_SECRET") from exc


def decrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    decrypted = dict(config)
    providers = []
    for provider in config.get("providers", []):
        item = dict(provider)
        if item.get("api_key"):
            item["api_key"] = decrypt_secret_value(str(item["api_key"]))
        if isinstance(item.get("api_keys"), list):
            item["api_keys"] = [decrypt_secret_value(str(key)) for key in item["api_keys"]]
        providers.append(item)
    decrypted["providers"] = providers
    return decrypted


def encrypt_config(config: dict[str, Any]) -> dict[str, Any]:
    if not encryption_fernet():
        return config
    encrypted = dict(config)
    providers = []
    for provider in config.get("providers", []):
        item = dict(provider)
        if item.get("api_key"):
            item["api_key"] = encrypt_secret_value(str(item["api_key"]))
        if isinstance(item.get("api_keys"), list):
            item["api_keys"] = [encrypt_secret_value(str(key)) for key in item["api_keys"]]
        providers.append(item)
    encrypted["providers"] = providers
    return encrypted


def write_config_file(config: dict[str, Any]) -> None:
    write_json_file(CONFIG_PATH, encrypt_config(config))


def load_state() -> dict[str, Any]:
    return read_json_file(STATE_PATH, {})


def save_state(state: dict[str, Any]) -> None:
    write_json_file(STATE_PATH, state)


def provider_api_keys(provider: dict[str, Any]) -> list[str]:
    keys = []
    api_keys = provider.get("api_keys")
    if isinstance(api_keys, list):
        keys.extend(str(key).strip() for key in api_keys if str(key).strip())
    if provider.get("api_key"):
        api_key = str(provider["api_key"]).strip()
        if api_key and api_key not in keys:
            keys.append(api_key)
    return keys


def key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def record_key_cooldown(provider_name: str, api_key: str) -> None:
    if KEY_COOLDOWN_SECONDS <= 0:
        return
    state = load_state()
    provider_state = state.setdefault(provider_name, {"calls": 0, "last_remaining": None})
    cooldowns = provider_state.setdefault("key_cooldowns", {})
    cooldowns[key_fingerprint(api_key)] = time.time() + KEY_COOLDOWN_SECONDS
    save_state(state)


def apply_env_overrides(provider: dict[str, Any]) -> dict[str, Any]:
    provider = dict(provider)
    env_key_name = provider.get("api_key_env")
    if env_key_name:
        provider["api_key"] = os.getenv(env_key_name, provider.get("api_key", ""))
    return provider


def load_raw_config() -> dict[str, Any]:
    config = decrypt_config(read_json_file(CONFIG_PATH, {"providers": [], "default_model": "gpt-3.5-turbo"}))
    config.setdefault("providers", [])
    config.setdefault("default_model", "gpt-3.5-turbo")
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


def request_log_entry(
    provider_name: str,
    status: int | str,
    started_at: float,
    fallback_count: int,
    error: str | None = None,
    streamed: bool = False,
    path: str = "/v1/chat/completions",
) -> dict[str, Any]:
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "provider": provider_name,
        "status": status,
        "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "fallback_count": fallback_count,
        "streamed": streamed,
        "error": error,
    }


def append_request_log(entry: dict[str, Any]) -> None:
    state = load_state()
    requests = state.setdefault("_requests", [])
    requests.insert(0, entry)
    del requests[REQUEST_LOG_LIMIT:]
    save_state(state)


def require_proxy_access(request: Request) -> None:
    if not PROXY_ACCESS_TOKEN:
        return
    auth_header = request.headers.get("authorization", "")
    x_api_key = request.headers.get("x-api-key", "")
    if auth_header == f"Bearer {PROXY_ACCESS_TOKEN}" or x_api_key == PROXY_ACCESS_TOKEN:
        return
    raise HTTPException(status_code=401, detail="Missing or invalid local proxy access token")


def enforce_rate_limit(request: Request) -> None:
    if RATE_LIMIT_PER_MINUTE <= 0:
        return
    client_host = request.client.host if request.client else "local"
    identity = request.headers.get("authorization") or request.headers.get("x-api-key") or client_host
    now = time.time()
    window_start = now - 60
    bucket = [timestamp for timestamp in RATE_LIMIT_BUCKETS.get(identity, []) if timestamp > window_start]
    if len(bucket) >= RATE_LIMIT_PER_MINUTE:
        RATE_LIMIT_BUCKETS[identity] = bucket
        raise HTTPException(status_code=429, detail="Local proxy rate limit exceeded")
    bucket.append(now)
    RATE_LIMIT_BUCKETS[identity] = bucket


async def read_v1_json_body(request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    enforce_rate_limit(request)
    raw = await request.body()
    if MAX_REQUEST_BYTES > 0 and len(raw) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request body is too large")
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc


def build_request_body(body: dict[str, Any], provider: dict[str, Any], default_model: str) -> dict[str, Any]:
    request_body = dict(body)
    aliases = provider.get("model_aliases") or {}
    requested_model = request_body.get("model", "")
    if requested_model and requested_model in aliases:
        request_body["model"] = aliases[requested_model]
    elif provider.get("model"):
        request_body["model"] = provider["model"]
    else:
        request_body.setdefault("model", default_model)
    return request_body


def build_forward_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def should_use_curl(provider: dict[str, Any]) -> bool:
    return bool(provider.get("use_curl"))


async def curl_request(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float = 30.0,
) -> tuple[int, str, dict[str, str]]:
    curl_headers = []
    for key, value in headers.items():
        curl_headers += ["-H", f"{key}: {value}"]
    cmd = [
        "curl.exe",
        "-s",
        "-S",
        "--max-time",
        str(int(timeout)),
        "-w",
        "\n%{http_code}",
        "-X",
        "POST",
        *curl_headers,
        "-d",
        json.dumps(body, ensure_ascii=False),
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace")
    if "\n" in output:
        *body_lines, status_line = output.split("\n")
        response_text = "\n".join(body_lines)
    else:
        status_line = output.strip()
        response_text = ""
    try:
        status_code = int(status_line.strip())
    except ValueError:
        status_code = 0
    return status_code, response_text, {}


async def forward_to_provider(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    provider: dict[str, Any],
    default_model: str,
    api_key: str,
) -> httpx.Response:
    url = f"{provider['base_url'].rstrip('/')}/chat/completions"
    headers = build_forward_headers(api_key)
    request_body = build_request_body(body, provider, default_model)
    return await client.post(url, headers=headers, json=request_body)


async def curl_forward_to_provider(
    body: dict[str, Any],
    provider: dict[str, Any],
    default_model: str,
    api_key: str,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    url = f"{provider['base_url'].rstrip('/')}/chat/completions"
    headers = build_forward_headers(api_key)
    request_body = build_request_body(body, provider, default_model)
    status_code, response_text, _ = await curl_request(url, headers, request_body, timeout)
    try:
        data = json.loads(response_text) if response_text else {}
    except json.JSONDecodeError:
        data = {"detail": response_text}
    return status_code, data


def safe_response_detail(response: httpx.Response) -> dict[str, Any]:
    try:
        return response.json()
    except json.JSONDecodeError:
        return {"detail": response.text}


async def check_provider(provider: dict[str, Any], default_model: str) -> dict[str, Any]:
    keys = provider_api_keys(provider)
    if not keys:
        return {"ok": False, "status": "no_api_key", "detail": "该 API 尚未填写密钥"}

    body = {
        "model": provider.get("model") or default_model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    api_key = keys[0]
    if should_use_curl(provider):
        try:
            status_code, data = await curl_forward_to_provider(body, provider, default_model, api_key, timeout=15.0)
        except Exception as exc:
            return {"ok": False, "status": "request_error", "detail": str(exc)}
        if status_code == 200:
            return {"ok": True, "status": 200, "detail": "Provider responded successfully"}
        return {"ok": False, "status": status_code, "detail": data}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await forward_to_provider(client, body, provider, default_model, api_key)
    except httpx.RequestError as exc:
        return {"ok": False, "status": "request_error", "detail": str(exc)}
    if response.status_code == 200:
        return {"ok": True, "status": 200, "detail": "Provider responded successfully"}
    return {"ok": False, "status": response.status_code, "detail": safe_response_detail(response)}


def normalize_provider(provider: dict[str, Any], existing_provider: dict[str, Any] | None = None) -> dict[str, Any]:
    name = str(provider.get("name", "")).strip()
    base_url = str(provider.get("base_url", "")).strip().rstrip("/")
    model = str(provider.get("model", "")).strip()
    api_key = str(provider.get("api_key", "")).strip()
    api_key_env = str(provider.get("api_key_env", "")).strip()
    use_curl = bool(provider.get("use_curl", False))
    enabled = bool(provider.get("enabled", True))
    model_aliases = provider.get("model_aliases") or {}
    if not isinstance(model_aliases, dict):
        model_aliases = {}

    if not name:
        raise ValueError("Provider name is required")
    if not base_url.startswith(("http://", "https://")):
        raise ValueError(f"Provider '{name}' base_url must start with http:// or https://")

    try:
        priority = int(provider.get("priority", 1000))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Provider '{name}' priority must be a number") from exc

    normalized: dict[str, Any] = {
        "name": name,
        "base_url": base_url,
        "model": model,
        "priority": priority,
        "enabled": enabled,
    }

    keys = []
    api_keys = provider.get("api_keys")
    if isinstance(api_keys, list):
        keys = [str(key).strip() for key in api_keys if str(key).strip()]
    if api_key:
        keys = [api_key]
    elif not keys and existing_provider:
        keys = provider_api_keys(existing_provider)
    if keys:
        normalized["api_keys"] = keys
        normalized["api_key"] = keys[0]

    if api_key_env:
        normalized["api_key_env"] = api_key_env
    elif existing_provider and existing_provider.get("api_key_env") and not keys:
        normalized["api_key_env"] = existing_provider["api_key_env"]

    if use_curl or (existing_provider and existing_provider.get("use_curl") and not api_key):
        normalized["use_curl"] = True

    if model_aliases:
        normalized["model_aliases"] = model_aliases

    return normalized


def normalize_config_payload(payload: dict[str, Any], existing_config: dict[str, Any] | None = None) -> dict[str, Any]:
    existing_config = existing_config or {"providers": []}
    existing_by_name = {
        provider.get("name"): provider
        for provider in existing_config.get("providers", [])
        if provider.get("name")
    }
    default_model = str(payload.get("default_model", "gpt-3.5-turbo")).strip() or "gpt-3.5-turbo"
    raw_providers = payload.get("providers", [])
    if not isinstance(raw_providers, list):
        raise ValueError("providers must be a list")

    providers = []
    seen_names = set()
    for provider in raw_providers:
        normalized = normalize_provider(provider, existing_by_name.get(provider.get("name")))
        if normalized["name"] in seen_names:
            raise ValueError(f"Provider '{normalized['name']}' is duplicated")
        seen_names.add(normalized["name"])
        providers.append(normalized)

    return {
        "providers": sorted(providers, key=lambda item: item.get("priority", 1000)),
        "default_model": default_model,
    }


def editable_provider(provider: dict[str, Any], state: dict[str, Any], default_model: str) -> dict[str, Any]:
    provider_state = state.get(provider.get("name", ""), {})
    resolved = apply_env_overrides(provider)
    keys = provider_api_keys(resolved)
    return {
        "name": provider.get("name", ""),
        "base_url": provider.get("base_url", ""),
        "model": provider.get("model", default_model),
        "priority": provider.get("priority", 1000),
        "api_key": "",
        "api_keys": [],
        "api_key_env": provider.get("api_key_env", ""),
        "has_api_key": bool(keys),
        "key_count": len(keys),
        "enabled": bool(provider.get("enabled", True)),
        "use_curl": bool(provider.get("use_curl", False)),
        "model_aliases": provider.get("model_aliases") or {},
        "calls": provider_state.get("calls", 0),
        "last_remaining": provider_state.get("last_remaining"),
    }


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
        await client.aclose()


async def stream_chat_completions(
    body: dict[str, Any],
    config: dict[str, Any],
    path: str = "/v1/chat/completions",
) -> StreamingResponse:
    last_error = "所有后端 API 均不可用"
    fallback_count = 0
    state = load_state()

    for provider in config["providers"]:
        provider_name = provider["name"]
        key_attempts = key_attempt_order(provider, state)
        for key_index, api_key in key_attempts:
            started_at = time.perf_counter()
            if should_use_curl(provider):
                try:
                    status_code, data = await curl_forward_to_provider(body, provider, config["default_model"], api_key)
                except Exception as exc:
                    last_error = str(exc)
                    append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error, True, path))
                    fallback_count += 1
                    continue

                logger.info("provider=%s status=%s path=%s", provider_name, status_code, path)
                log_entry = request_log_entry(provider_name, status_code, started_at, fallback_count, streamed=True, path=path)
                log_entry["stream_status"] = "stream_complete"
                append_request_log(log_entry)
                if status_code == 200:
                    record_success(provider_name, httpx.Response(200, json={}), len(key_attempts), key_index)
                    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                    return StreamingResponse(iter([payload.encode("utf-8")]), media_type="text/event-stream")
                if status_code in RETRYABLE_STATUS_CODES:
                    if status_code == 429:
                        record_key_cooldown(provider_name, api_key)
                    last_error = json.dumps(data, ensure_ascii=False)
                    fallback_count += 1
                    continue
                raise HTTPException(status_code=status_code, detail=data)

            client = create_http_client()
            try:
                url = f"{provider['base_url'].rstrip('/')}/chat/completions"
                headers = build_forward_headers(api_key)
                request_body = build_request_body(body, provider, config["default_model"])
                upstream_request = client.build_request("POST", url, headers=headers, json=request_body)
                response = await client.send(upstream_request, stream=True)
            except httpx.RequestError as exc:
                await client.aclose()
                last_error = str(exc)
                append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error, True, path))
                fallback_count += 1
                continue

            logger.info("provider=%s status=%s path=%s", provider_name, response.status_code, path)
            if response.status_code == 200:
                record_success(provider_name, response, len(key_attempts), key_index)
                log_entry = request_log_entry(provider_name, response.status_code, started_at, fallback_count, streamed=True, path=path)
                return StreamingResponse(
                    stream_response_bytes(response, client, log_entry),
                    media_type=response.headers.get("content-type", "text/event-stream"),
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

            error_bytes = await response.aread()
            await response.aclose()
            await client.aclose()
            error_text = error_bytes.decode("utf-8", errors="replace")
            append_request_log(request_log_entry(provider_name, response.status_code, started_at, fallback_count, error_text, True, path))
            if response.status_code in RETRYABLE_STATUS_CODES:
                if response.status_code == 429:
                    record_key_cooldown(provider_name, api_key)
                last_error = error_text
                fallback_count += 1
                continue
            raise HTTPException(status_code=response.status_code, detail=error_text)

    raise HTTPException(status_code=502, detail=last_error)


async def proxy_chat_json(
    body: dict[str, Any],
    config: dict[str, Any],
    path: str = "/v1/chat/completions",
) -> JSONResponse:
    last_error = "所有后端 API 均不可用"
    fallback_count = 0
    state = load_state()

    async with create_http_client() as client:
        for provider in config["providers"]:
            provider_name = provider["name"]
            key_attempts = key_attempt_order(provider, state)
            for key_index, api_key in key_attempts:
                started_at = time.perf_counter()
                if should_use_curl(provider):
                    try:
                        status_code, data = await curl_forward_to_provider(body, provider, config["default_model"], api_key)
                    except Exception as exc:
                        logger.info("provider=%s status=request_error path=%s", provider_name, path)
                        last_error = str(exc)
                        append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error, path=path))
                        fallback_count += 1
                        continue

                    logger.info("provider=%s status=%s path=%s", provider_name, status_code, path)
                    append_request_log(request_log_entry(provider_name, status_code, started_at, fallback_count, path=path))
                    if status_code == 200:
                        record_success(provider_name, httpx.Response(200, json=data), len(key_attempts), key_index)
                        return JSONResponse(content=data, status_code=200)
                    if status_code in RETRYABLE_STATUS_CODES:
                        if status_code == 429:
                            record_key_cooldown(provider_name, api_key)
                        last_error = json.dumps(data, ensure_ascii=False)
                        fallback_count += 1
                        continue
                    return JSONResponse(content=data, status_code=status_code)

                try:
                    response = await forward_to_provider(client, body, provider, config["default_model"], api_key)
                except httpx.RequestError as exc:
                    logger.info("provider=%s status=request_error path=%s", provider_name, path)
                    last_error = str(exc)
                    append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error, path=path))
                    fallback_count += 1
                    continue

                logger.info("provider=%s status=%s path=%s", provider_name, response.status_code, path)
                append_request_log(request_log_entry(provider_name, response.status_code, started_at, fallback_count, path=path))
                if response.status_code == 200:
                    record_success(provider_name, response, len(key_attempts), key_index)
                    return JSONResponse(content=response.json(), status_code=200)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    if response.status_code == 429:
                        record_key_cooldown(provider_name, api_key)
                    last_error = response.text
                    fallback_count += 1
                    continue
                return JSONResponse(content=safe_response_detail(response), status_code=response.status_code)

    raise HTTPException(status_code=502, detail=last_error)


def response_text_from_chat(chat_data: dict[str, Any]) -> str:
    try:
        return chat_data.get("choices", [{}])[0].get("message", {}).get("content") or ""
    except (AttributeError, IndexError):
        return ""


def responses_input_to_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    input_value = payload.get("input", "")
    if isinstance(input_value, str):
        return [{"role": "user", "content": input_value}]
    if isinstance(input_value, list):
        messages = []
        for item in input_value:
            if isinstance(item, dict) and item.get("role") and "content" in item:
                messages.append({"role": item["role"], "content": item["content"]})
            else:
                messages.append({"role": "user", "content": json.dumps(item, ensure_ascii=False)})
        return messages or [{"role": "user", "content": ""}]
    return [{"role": "user", "content": json.dumps(input_value, ensure_ascii=False)}]


def chat_to_responses_payload(chat_data: dict[str, Any], requested_model: str | None) -> dict[str, Any]:
    text = response_text_from_chat(chat_data)
    model = chat_data.get("model") or requested_model or ""
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "output": [
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "output_text": text,
        "usage": chat_data.get("usage"),
    }


async def fetch_provider_models(provider: dict[str, Any]) -> list[dict[str, Any]]:
    url = f"{provider['base_url'].rstrip('/')}/models"
    api_key = provider_api_keys(provider)[0]
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if should_use_curl(provider):
        cmd = ["curl.exe", "-s", "-S", "--max-time", "15", "-H", f"Authorization: Bearer {api_key}", url]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    else:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers)
        if response.status_code != 200:
            return []
        data = response.json()

    models = data.get("data", []) if isinstance(data, dict) else []
    return [model for model in models if isinstance(model, dict) and model.get("id")]


@app.get("/")
def dashboard() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard files are missing")
    return FileResponse(index_path)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await read_v1_json_body(request)
    config = load_config()
    if not config["providers"]:
        raise HTTPException(status_code=500, detail="No usable providers configured")
    if body.get("stream") is True:
        return await stream_chat_completions(body, config)
    return await proxy_chat_json(body, config)


@app.post("/v1/responses")
async def responses(request: Request):
    payload = await read_v1_json_body(request)
    config = load_config()
    if not config["providers"]:
        raise HTTPException(status_code=500, detail="No usable providers configured")

    chat_body = {
        "model": payload.get("model", config["default_model"]),
        "messages": responses_input_to_messages(payload),
    }
    for key in ("temperature", "top_p", "max_tokens", "max_completion_tokens", "stream"):
        if key in payload:
            chat_body[key] = payload[key]

    if chat_body.get("stream") is True:
        return await stream_chat_completions(chat_body, config, path="/v1/responses")

    chat_response = await proxy_chat_json(chat_body, config, path="/v1/responses")
    if chat_response.status_code != 200:
        return chat_response
    chat_data = json.loads(chat_response.body.decode("utf-8"))
    return JSONResponse(content=chat_to_responses_payload(chat_data, payload.get("model")), status_code=200)


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
def health() -> dict[str, str]:
    return {"status": "ok"}


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


@app.get("/api/providers/{provider_name}/models")
async def provider_models(provider_name: str, request: Request) -> dict[str, Any]:
    require_proxy_access(request)
    config = load_config()
    for provider in config["providers"]:
        if provider["name"] == provider_name:
            models = await fetch_provider_models(provider)
            return {"provider": provider_name, "status": 200, "models": {"object": "list", "data": models}}
    raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
