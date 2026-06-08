import asyncio
import json
import shutil
from typing import Any, AsyncIterator

import httpx

from ._config import provider_api_keys


def _curl_bin() -> str:
    return shutil.which("curl") or "curl"


VALID_PROTOCOLS = {"openai", "domestic", "claude", "gemini"}
ANTHROPIC_VERSION = "2023-06-01"
GEMINI_API_VERSION = "v1beta"

# Domestic model providers that use OpenAI-compatible format
# but may have different authentication or parameter requirements
DOMESTIC_PROVIDERS = {
    "qwen",      # 通义千问
    "ernie",     # 文心一言
    "glm",       # 智谱 ChatGLM
    "baichuan",  # 百川
    "doubao",    # 豆包
    "hunyuan",   # 混元
    "yi",        # 零一万物
    "deepseek",  # DeepSeek
}


def provider_protocol(provider: dict[str, Any]) -> str:
    protocol = str(provider.get("protocol", "openai")).strip().lower() or "openai"
    return protocol if protocol in VALID_PROTOCOLS else "openai"


def resolve_model(body: dict[str, Any], provider: dict[str, Any], default_model: str) -> str:
    """Resolve the effective model name applying aliases / provider override."""
    aliases = provider.get("model_aliases") or {}
    requested_model = body.get("model", "") if isinstance(body, dict) else ""
    if requested_model and requested_model in aliases:
        return aliases[requested_model]
    if provider.get("model"):
        return provider["model"]
    return requested_model or default_model


def passthrough_url(provider: dict[str, Any], path_suffix: str) -> str:
    """Forward to the backend using the path suffix decided by the inbound endpoint.

    path_suffix examples: "/chat/completions", "/responses", "/messages",
    "/models/{model}:generateContent". Already includes the leading slash.
    """
    base = provider["base_url"].rstrip("/")
    return f"{base}{path_suffix}"


def passthrough_headers(protocol: str, api_key: str) -> dict[str, str]:
    """Generate protocol-specific authentication headers.

    Supports:
    - claude: uses x-api-key header
    - gemini: uses x-goog-api-key header
    - openai/domestic: uses Authorization Bearer token
    """
    if protocol == "claude":
        return {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Content-Type": "application/json",
        }
    if protocol == "gemini":
        return {
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        }
    # OpenAI and domestic models use Bearer token authentication
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def passthrough_body(body: dict[str, Any], provider: dict[str, Any], default_model: str, protocol: str) -> Any:
    """Forward the client body verbatim, only rewriting the model field where it lives in the body.

    For gemini the model lives in the URL, so the body is left completely untouched.
    For openai/domestic/claude the model lives in the body — reuse build_request_body,
    which copies the body and only touches the `model` field (alias / provider override).
    """
    if protocol == "gemini":
        return dict(body) if isinstance(body, dict) else body
    if not isinstance(body, dict):
        return body
    return build_request_body(body, provider, default_model)


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
) -> tuple[int, str]:
    curl_headers = []
    for key, value in headers.items():
        curl_headers += ["-H", f"{key}: {value}"]
    cmd = [
        _curl_bin(),
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
    return status_code, response_text


async def curl_stream_request(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: float = 120.0,
) -> AsyncIterator[bytes]:
    curl_headers = []
    for key, value in headers.items():
        curl_headers += ["-H", f"{key}: {value}"]
    cmd = [
        _curl_bin(),
        "--no-buffer",
        "-s",
        "--max-time",
        str(int(timeout)),
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
    try:
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            yield chunk
    finally:
        proc.kill()
        await proc.wait()


def _prepare_forward(
    body: dict[str, Any],
    provider: dict[str, Any],
    default_model: str,
    api_key: str,
    protocol: str,
    path_suffix: str,
    passthrough: bool,
) -> tuple[str, dict[str, str], Any]:
    """Compute (url, headers, request_body) for one forward call.

    passthrough=False keeps the legacy OpenAI behavior (chat/completions + Bearer
    + model-rewriting build_request_body) so existing call sites are unchanged.
    passthrough=True forwards the client body verbatim (only rewriting body.model
    where applicable) to the protocol-specific URL with protocol-specific auth.
    """
    if not passthrough:
        url = f"{provider['base_url'].rstrip('/')}/chat/completions"
        return url, build_forward_headers(api_key), build_request_body(body, provider, default_model)

    model = resolve_model(body, provider, default_model)
    resolved_suffix = path_suffix.replace("{model}", model) if path_suffix else "/chat/completions"
    url = passthrough_url(provider, resolved_suffix)
    headers = passthrough_headers(protocol, api_key)
    request_body = passthrough_body(body, provider, default_model, protocol)
    return url, headers, request_body


async def forward_to_provider(
    client: httpx.AsyncClient,
    body: dict[str, Any],
    provider: dict[str, Any],
    default_model: str,
    api_key: str,
    protocol: str = "openai",
    path_suffix: str = "/chat/completions",
    passthrough: bool = False,
) -> httpx.Response:
    url, headers, request_body = _prepare_forward(
        body, provider, default_model, api_key, protocol, path_suffix, passthrough
    )
    return await client.post(url, headers=headers, json=request_body)


async def curl_forward_to_provider(
    body: dict[str, Any],
    provider: dict[str, Any],
    default_model: str,
    api_key: str,
    timeout: float = 30.0,
    protocol: str = "openai",
    path_suffix: str = "/chat/completions",
    passthrough: bool = False,
) -> tuple[int, Any]:
    url, headers, request_body = _prepare_forward(
        body, provider, default_model, api_key, protocol, path_suffix, passthrough
    )
    status_code, response_text = await curl_request(url, headers, request_body, timeout)
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


def _check_request(provider: dict[str, Any], default_model: str) -> tuple[str, str, dict[str, Any]]:
    """Build a minimal protocol-appropriate ping request.

    Returns (protocol, path_suffix, body). Gemini carries the model in the URL and
    uses a `contents` body; the other protocols carry the model in the body.
    """
    protocol = provider_protocol(provider)
    model = provider.get("model") or default_model or "gpt-3.5-turbo"
    if protocol == "gemini":
        return protocol, "/models/{model}:generateContent", {
            "contents": [{"parts": [{"text": "ping"}]}],
            "generationConfig": {"maxOutputTokens": 1},
        }
    return protocol, "/messages" if protocol == "claude" else "/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }


async def check_provider(provider: dict[str, Any], default_model: str) -> dict[str, Any]:
    keys = provider_api_keys(provider)
    if not keys:
        return {"ok": False, "status": "no_api_key", "detail": "该 API 尚未填写密钥"}

    protocol, path_suffix, body = _check_request(provider, default_model)
    api_key = keys[0]
    if should_use_curl(provider):
        try:
            status_code, data = await curl_forward_to_provider(
                body, provider, default_model, api_key, timeout=15.0,
                protocol=protocol, path_suffix=path_suffix, passthrough=True,
            )
        except Exception as exc:
            return {"ok": False, "status": "request_error", "detail": str(exc)}
        if status_code == 200:
            return {"ok": True, "status": 200, "detail": "Provider responded successfully"}
        return {"ok": False, "status": status_code, "detail": data}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            response = await forward_to_provider(
                client, body, provider, default_model, api_key,
                protocol=protocol, path_suffix=path_suffix, passthrough=True,
            )
    except httpx.RequestError as exc:
        return {"ok": False, "status": "request_error", "detail": str(exc)}
    if response.status_code == 200:
        return {"ok": True, "status": 200, "detail": "Provider responded successfully"}
    return {"ok": False, "status": response.status_code, "detail": safe_response_detail(response)}


async def fetch_provider_models(provider: dict[str, Any]) -> list[dict[str, Any]]:
    # Only OpenAI-compatible protocols expose a Bearer-authenticated /models list
    # in the expected {"data": [...]} shape. For claude/gemini the caller falls
    # back to the configured provider["model"].
    if provider_protocol(provider) not in {"openai", "domestic"}:
        return []
    url = f"{provider['base_url'].rstrip('/')}/models"
    api_key = provider_api_keys(provider)[0]
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if should_use_curl(provider):
        cmd = [_curl_bin(), "-s", "-S", "--max-time", "15", "-H", f"Authorization: Bearer {api_key}", url]
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
