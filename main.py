import json
import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
CONFIG_PATH = Path(os.getenv("GPT_PROXY_CONFIG", BASE_DIR / "config.json"))
STATE_PATH = Path(os.getenv("GPT_PROXY_STATE", BASE_DIR / "state.json"))
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}

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
        if provider.get("name") and provider.get("base_url") and provider.get("api_key")
    ]
    config["providers"] = sorted(providers, key=lambda item: item.get("priority", 1000))
    return config


def load_state() -> dict[str, Any]:
    return read_json_file(STATE_PATH, {})


def record_success(provider_name: str, response: httpx.Response) -> None:
    state = load_state()
    provider_state = state.setdefault(provider_name, {"calls": 0, "last_remaining": None})
    provider_state["calls"] = int(provider_state.get("calls", 0)) + 1

    remaining = response.headers.get("x-ratelimit-remaining")
    if remaining is not None:
        try:
            provider_state["last_remaining"] = int(remaining)
        except ValueError:
            provider_state["last_remaining"] = remaining

    write_json_file(STATE_PATH, state)


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


async def curl_request(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float = 30.0) -> tuple[int, str, dict[str, str]]:
    curl_headers = []
    for key, value in headers.items():
        curl_headers += ["-H", f"{key}: {value}"]
    cmd = [
        "curl.exe", "-s", "-S",
        "--max-time", str(int(timeout)),
        "-w", "\n%{http_code}",
        "-X", "POST",
        *curl_headers,
        "-d", json.dumps(body, ensure_ascii=False),
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
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


def should_use_curl(provider: dict[str, Any]) -> bool:
    return bool(provider.get("use_curl"))


def build_forward_headers(provider: dict[str, Any]) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {provider['api_key']}",
        "Content-Type": "application/json",
    }


async def forward_to_provider(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    provider: dict[str, Any],
    default_model: str,
) -> httpx.Response:
    url = f"{provider['base_url'].rstrip('/')}/chat/completions"
    headers = build_forward_headers(provider)
    request_body = build_request_body(body, provider, default_model)
    return await client.post(url, headers=headers, json=request_body)


async def curl_forward_to_provider(
    body: dict[str, Any],
    provider: dict[str, Any],
    default_model: str,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    url = f"{provider['base_url'].rstrip('/')}/chat/completions"
    headers = build_forward_headers(provider)
    request_body = build_request_body(body, provider, default_model)
    status_code, response_text, _ = await curl_request(url, headers, request_body, timeout)
    try:
        data = json.loads(response_text) if response_text else {}
    except json.JSONDecodeError:
        data = {"detail": response_text}
    return status_code, data


async def check_provider(provider: dict[str, Any], default_model: str) -> dict[str, Any]:
    body = {
        "model": provider.get("model") or default_model or "gpt-3.5-turbo",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }
    if should_use_curl(provider):
        try:
            status_code, data = await curl_forward_to_provider(body, provider, default_model, timeout=15.0)
        except Exception as exc:
            return {"ok": False, "status": "request_error", "detail": str(exc)}
        if status_code == 200:
            return {"ok": True, "status": 200, "detail": "Provider responded successfully"}
        return {"ok": False, "status": status_code, "detail": data}
    else:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await forward_to_provider(client, body, provider, default_model)
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
    }

    if api_key:
        normalized["api_key"] = api_key
    elif existing_provider and existing_provider.get("api_key"):
        normalized["api_key"] = existing_provider["api_key"]

    if api_key_env:
        normalized["api_key_env"] = api_key_env
    elif existing_provider and existing_provider.get("api_key_env") and not api_key:
        normalized["api_key_env"] = existing_provider["api_key_env"]

    if use_curl or (existing_provider and existing_provider.get("use_curl") and not api_key):
        normalized["use_curl"] = True

    if model_aliases:
        normalized["model_aliases"] = model_aliases

    return normalized


def editable_provider(provider: dict[str, Any], state: dict[str, Any], default_model: str) -> dict[str, Any]:
    provider_state = state.get(provider.get("name", ""), {})
    resolved = apply_env_overrides(provider)
    return {
        "name": provider.get("name", ""),
        "base_url": provider.get("base_url", ""),
        "model": provider.get("model", default_model),
        "priority": provider.get("priority", 1000),
        "api_key": "",
        "api_key_env": provider.get("api_key_env", ""),
        "has_api_key": bool(resolved.get("api_key")),
        "use_curl": bool(provider.get("use_curl", False)),
        "model_aliases": provider.get("model_aliases") or {},
        "calls": provider_state.get("calls", 0),
        "last_remaining": provider_state.get("last_remaining"),
    }


def safe_response_detail(response: httpx.Response) -> dict[str, Any]:
    try:
        return response.json()
    except json.JSONDecodeError:
        return {"detail": response.text}


@app.get("/")
def dashboard() -> FileResponse:
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard files are missing")
    return FileResponse(index_path)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    config = load_config()
    providers = config["providers"]
    if not providers:
        raise HTTPException(status_code=500, detail="No usable providers configured")

    try:
        body = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON request body") from exc

    last_error = "All backend providers failed"
    curl_providers = [p for p in providers if should_use_curl(p)]
    httpx_providers = [p for p in providers if not should_use_curl(p)]

    for provider in curl_providers:
        provider_name = provider["name"]
        try:
            status_code, data = await curl_forward_to_provider(body, provider, config["default_model"])
        except Exception as exc:
            logger.info("provider=%s status=request_error path=/v1/chat/completions", provider_name)
            last_error = str(exc)
            continue
        logger.info("provider=%s status=%s path=/v1/chat/completions", provider_name, status_code)
        if status_code == 200:
            record_success(provider_name, httpx.Response(200, json=data))
            return JSONResponse(content=data, status_code=200)
        if status_code in RETRYABLE_STATUS_CODES:
            last_error = json.dumps(data)
            continue
        return JSONResponse(content=data, status_code=status_code)

    async with create_http_client() as client:
        for provider in httpx_providers:
            provider_name = provider["name"]
            try:
                response = await forward_to_provider(client, body, provider, config["default_model"])
            except httpx.RequestError as exc:
                logger.info("provider=%s status=request_error path=/v1/chat/completions", provider_name)
                last_error = str(exc)
                continue
            logger.info("provider=%s status=%s path=/v1/chat/completions", provider_name, response.status_code)
            if response.status_code == 200:
                record_success(provider_name, response)
                return JSONResponse(content=response.json(), status_code=200)
            if response.status_code in RETRYABLE_STATUS_CODES:
                last_error = response.text
                continue
            return JSONResponse(content=safe_response_detail(response), status_code=response.status_code)

    raise HTTPException(status_code=502, detail=last_error or "All backend providers failed")


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
                "calls": state.get(provider["name"], {}).get("calls", 0),
                "last_remaining": state.get(provider["name"], {}).get("last_remaining"),
            }
        )
    return {"providers": providers}


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
            "detail": "该 API 尚未填写密钥，请先在 UI 中输入 API Key 并保存",
        }
    raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' does not exist in config")


@app.get("/api/providers/{provider_name}/models")
async def provider_models(provider_name: str) -> dict[str, Any]:
    config = load_config()
    for provider in config["providers"]:
        if provider["name"] == provider_name:
            url = f"{provider['base_url'].rstrip('/')}/models"
            headers = {"Authorization": f"Bearer {provider['api_key']}", "Accept": "application/json"}
            if should_use_curl(provider):
                cmd = ["curl.exe", "-s", "-S", "--max-time", "15", "-H", f"Authorization: Bearer {provider['api_key']}", url]
                proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, _ = await proc.communicate()
                try:
                    data = json.loads(stdout.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    data = {"raw": stdout.decode("utf-8", errors="replace")}
                return {"provider": provider_name, "models": data}
            else:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.get(url, headers=headers)
                return {"provider": provider_name, "status": resp.status_code, "models": resp.json()}
    raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' not found")
