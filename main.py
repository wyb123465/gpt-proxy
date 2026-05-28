import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = Path(os.getenv("GPT_PROXY_CONFIG", BASE_DIR / "config.json"))
STATE_PATH = Path(os.getenv("GPT_PROXY_STATE", BASE_DIR / "state.json"))
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
REQUEST_LOG_LIMIT = 50

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


def apply_env_overrides(provider: dict[str, Any]) -> dict[str, Any]:
    provider = dict(provider)
    env_key_name = provider.get("api_key_env")
    if env_key_name:
        provider["api_key"] = os.getenv(env_key_name, provider.get("api_key", ""))
    return provider


def load_raw_config() -> dict[str, Any]:
    config = read_json_file(CONFIG_PATH, {"providers": [], "default_model": "gpt-3.5-turbo"})
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
    return [(index, keys[index]) for index in ordered_indexes]


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
) -> dict[str, Any]:
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "path": "/v1/chat/completions",
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


async def stream_response_bytes(response: httpx.Response, client: httpx.AsyncClient) -> AsyncIterator[bytes]:
    try:
        async for chunk in response.aiter_bytes():
            if chunk:
                yield chunk
    finally:
        await response.aclose()
        await client.aclose()


async def stream_chat_completions(body: dict[str, Any], config: dict[str, Any]) -> StreamingResponse:
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
                    append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error, True))
                    fallback_count += 1
                    continue

                logger.info("provider=%s status=%s path=/v1/chat/completions", provider_name, status_code)
                append_request_log(request_log_entry(provider_name, status_code, started_at, fallback_count, streamed=True))
                if status_code == 200:
                    record_success(provider_name, httpx.Response(200, json={}), len(key_attempts), key_index)
                    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                    return StreamingResponse(iter([payload.encode("utf-8")]), media_type="text/event-stream")
                if status_code in RETRYABLE_STATUS_CODES:
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
                append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error, True))
                fallback_count += 1
                continue

            logger.info("provider=%s status=%s path=/v1/chat/completions", provider_name, response.status_code)
            if response.status_code == 200:
                record_success(provider_name, response, len(key_attempts), key_index)
                append_request_log(request_log_entry(provider_name, response.status_code, started_at, fallback_count, streamed=True))
                return StreamingResponse(
                    stream_response_bytes(response, client),
                    media_type=response.headers.get("content-type", "text/event-stream"),
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )

            error_bytes = await response.aread()
            await response.aclose()
            await client.aclose()
            error_text = error_bytes.decode("utf-8", errors="replace")
            append_request_log(request_log_entry(provider_name, response.status_code, started_at, fallback_count, error_text, True))
            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = error_text
                fallback_count += 1
                continue
            raise HTTPException(status_code=response.status_code, detail=error_text)

    raise HTTPException(status_code=502, detail=last_error)


@app.get("/")
def dashboard() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard files are missing")
    return FileResponse(index_path)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    config = load_config()
    providers = config["providers"]
    if not providers:
        raise HTTPException(status_code=500, detail="No usable providers configured")

    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc

    if body.get("stream") is True:
        return await stream_chat_completions(body, config)

    last_error = "所有后端 API 均不可用"
    fallback_count = 0
    state = load_state()

    async with create_http_client() as client:
        for provider in providers:
            provider_name = provider["name"]
            key_attempts = key_attempt_order(provider, state)
            for key_index, api_key in key_attempts:
                started_at = time.perf_counter()
                if should_use_curl(provider):
                    try:
                        status_code, data = await curl_forward_to_provider(body, provider, config["default_model"], api_key)
                    except Exception as exc:
                        logger.info("provider=%s status=request_error path=/v1/chat/completions", provider_name)
                        last_error = str(exc)
                        append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error))
                        fallback_count += 1
                        continue

                    logger.info("provider=%s status=%s path=/v1/chat/completions", provider_name, status_code)
                    append_request_log(request_log_entry(provider_name, status_code, started_at, fallback_count))
                    if status_code == 200:
                        record_success(provider_name, httpx.Response(200, json=data), len(key_attempts), key_index)
                        return JSONResponse(content=data, status_code=200)
                    if status_code in RETRYABLE_STATUS_CODES:
                        last_error = json.dumps(data, ensure_ascii=False)
                        fallback_count += 1
                        continue
                    return JSONResponse(content=data, status_code=status_code)

                try:
                    response = await forward_to_provider(client, body, provider, config["default_model"], api_key)
                except httpx.RequestError as exc:
                    logger.info("provider=%s status=request_error path=/v1/chat/completions", provider_name)
                    last_error = str(exc)
                    append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error))
                    fallback_count += 1
                    continue

                logger.info("provider=%s status=%s path=/v1/chat/completions", provider_name, response.status_code)
                append_request_log(request_log_entry(provider_name, response.status_code, started_at, fallback_count))
                if response.status_code == 200:
                    record_success(provider_name, response, len(key_attempts), key_index)
                    return JSONResponse(content=response.json(), status_code=200)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    last_error = response.text
                    fallback_count += 1
                    continue
                return JSONResponse(content=safe_response_detail(response), status_code=response.status_code)

    raise HTTPException(status_code=502, detail=last_error)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    config = load_raw_config()
    state = load_state()
    return {
        "default_model": config.get("default_model", "gpt-3.5-turbo"),
        "providers": [
            editable_provider(provider, state, config.get("default_model", "gpt-3.5-turbo"))
            for provider in sorted(config.get("providers", []), key=lambda item: item.get("priority", 1000))
        ],
    }


@app.post("/api/config")
async def save_config(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc

    existing_config = load_raw_config()
    existing_by_name = {
        provider.get("name"): provider
        for provider in existing_config.get("providers", [])
        if provider.get("name")
    }

    default_model = str(payload.get("default_model", "gpt-3.5-turbo")).strip() or "gpt-3.5-turbo"
    raw_providers = payload.get("providers", [])
    if not isinstance(raw_providers, list):
        raise HTTPException(status_code=400, detail="providers must be a list")

    providers = []
    seen_names = set()
    try:
        for provider in raw_providers:
            normalized = normalize_provider(provider, existing_by_name.get(provider.get("name")))
            if normalized["name"] in seen_names:
                raise ValueError(f"Provider '{normalized['name']}' is duplicated")
            seen_names.add(normalized["name"])
            providers.append(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    providers = sorted(providers, key=lambda item: item.get("priority", 1000))
    write_json_file(CONFIG_PATH, {"providers": providers, "default_model": default_model})
    return get_config()


@app.get("/api/providers")
def provider_status() -> dict[str, Any]:
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
def recent_requests() -> dict[str, Any]:
    state = load_state()
    return {"requests": state.get("_requests", [])[:REQUEST_LOG_LIMIT]}


@app.post("/api/providers/{provider_name}/check")
async def provider_check(provider_name: str) -> dict[str, Any]:
    config = load_config()
    raw_config = load_raw_config()
    raw_names = {p.get("name") for p in raw_config.get("providers", []) if p.get("name")}
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
async def provider_models(provider_name: str) -> dict[str, Any]:
    config = load_config()
    for provider in config["providers"]:
        if provider["name"] == provider_name:
            url = f"{provider['base_url'].rstrip('/')}/models"
            api_key = provider_api_keys(provider)[0]
            headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
            if should_use_curl(provider):
                cmd = ["curl.exe", "-s", "-S", "--max-time", "15", "-H", f"Authorization: Bearer {api_key}", url]
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, _ = await proc.communicate()
                try:
                    data = json.loads(stdout.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    data = {"raw": stdout.decode("utf-8", errors="replace")}
                return {"provider": provider_name, "models": data}

            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
            return {"provider": provider_name, "status": resp.status_code, "models": resp.json()}
    raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
