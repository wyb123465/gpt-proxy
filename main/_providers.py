import asyncio
import json
import shutil
from typing import Any, AsyncIterator

import httpx

from ._config import provider_api_keys, safe_bool, safe_model_aliases, safe_provider_model


def _curl_bin() -> str:
    return shutil.which("curl") or "curl"


PROTOCOL_CATALOG: dict[str, dict[str, str]] = {
    "openai": {
        "label": "OpenAI",
        "group": "OpenAI 协议",
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "chat_endpoint": "/v1/chat/completions",
        "native_endpoint": "/v1/responses",
        "auth": "Authorization: Bearer",
        "model_fetch": "GET /v1/models",
        "description": "适合 OpenAI 官方，以及完整兼容 OpenAI API 的服务。",
    },
    "domestic": {
        "label": "国内大模型",
        "group": "国内 OpenAI 兼容",
        "default_base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "chat_endpoint": "/v1/chat/completions",
        "native_endpoint": "/v1/chat/completions",
        "auth": "Authorization: Bearer",
        "model_fetch": "GET /v1/models（若服务商支持）",
        "description": "适合 DeepSeek、通义千问兼容模式、智谱等 OpenAI 兼容入口。",
    },
    "claude": {
        "label": "Claude",
        "group": "Claude 原生协议",
        "default_base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-sonnet-4-20250514",
        "chat_endpoint": "/v1/messages",
        "native_endpoint": "/v1/messages",
        "auth": "x-api-key + anthropic-version",
        "model_fetch": "GET /v1/models",
        "description": "适合 Anthropic Claude Messages API，使用 Claude 原生请求体。",
    },
    "gemini": {
        "label": "Gemini",
        "group": "Gemini 原生协议",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.0-flash",
        "chat_endpoint": "/v1beta/models/{model}:generateContent",
        "native_endpoint": "/v1beta/models/{model}:generateContent",
        "auth": "x-goog-api-key",
        "model_fetch": "GET /v1beta/models",
        "description": "适合 Google Gemini API，使用 contents/parts 原生格式。",
    },
}

PROVIDER_PRESETS: list[dict[str, Any]] = [
    {
        "id": "openai",
        "name": "OpenAI",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "description": "OpenAI 官方 API",
        "website": "https://platform.openai.com/",
        "api_key_url": "https://platform.openai.com/api-keys",
        "model_aliases": {"gpt-4": "gpt-4o", "gpt-3.5-turbo": "gpt-4o-mini"},
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "protocol": "domestic",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "description": "DeepSeek OpenAI 兼容入口",
        "website": "https://platform.deepseek.com/",
        "api_key_url": "https://platform.deepseek.com/api_keys",
        "model_aliases": {"gpt-4o": "deepseek-chat", "gpt-3.5-turbo": "deepseek-chat"},
    },
    {
        "id": "qwen",
        "name": "通义千问",
        "protocol": "domestic",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-max",
        "description": "阿里百炼 DashScope 兼容模式",
        "website": "https://bailian.console.aliyun.com/",
        "model_aliases": {"gpt-4o": "qwen-max", "gpt-3.5-turbo": "qwen-plus"},
    },
    {
        "id": "glm",
        "name": "智谱 GLM",
        "protocol": "domestic",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-plus",
        "description": "智谱 BigModel OpenAI 兼容入口",
        "website": "https://open.bigmodel.cn/",
        "model_aliases": {"gpt-4o": "glm-4-plus", "gpt-3.5-turbo": "glm-4-flash"},
    },
    {
        "id": "moonshot",
        "name": "Moonshot / Kimi",
        "protocol": "domestic",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "kimi-k2.6",
        "description": "Moonshot OpenAI 兼容入口",
        "website": "https://platform.moonshot.cn/",
        "model_aliases": {"gpt-4o": "kimi-k2.6"},
    },
    {
        "id": "siliconflow",
        "name": "SiliconFlow",
        "protocol": "domestic",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen3-Coder-480B-A35B-Instruct",
        "description": "硅基流动 OpenAI 兼容入口",
        "website": "https://cloud.siliconflow.cn/",
        "model_aliases": {"gpt-4o": "Qwen/Qwen3-Coder-480B-A35B-Instruct"},
    },
    {
        "id": "openrouter",
        "name": "OpenRouter",
        "protocol": "domestic",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "openai/gpt-4o-mini",
        "description": "OpenRouter 多模型聚合入口",
        "website": "https://openrouter.ai/",
        "api_key_url": "https://openrouter.ai/keys",
        "model_aliases": {"gpt-4o": "openai/gpt-4o", "gpt-3.5-turbo": "openai/gpt-4o-mini"},
    },
    {
        "id": "aihubmix",
        "name": "AiHubMix",
        "protocol": "domestic",
        "base_url": "https://aihubmix.com/v1",
        "model": "gpt-4o-mini",
        "description": "AiHubMix OpenAI 兼容入口",
        "website": "https://aihubmix.com/",
    },
    {
        "id": "doubao",
        "name": "豆包 / 火山方舟",
        "protocol": "domestic",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-1-6",
        "description": "火山方舟 OpenAI 兼容入口",
        "website": "https://www.volcengine.com/product/ark",
        "model_aliases": {"gpt-4o": "doubao-seed-1-6"},
    },
    {
        "id": "baidu_qianfan",
        "name": "百度千帆",
        "protocol": "domestic",
        "base_url": "https://qianfan.baidubce.com/v2",
        "model": "ernie-4.5-turbo-128k",
        "description": "百度千帆 OpenAI 兼容入口",
        "website": "https://qianfan.cloud.baidu.com/",
    },
    {
        "id": "minimax",
        "name": "MiniMax",
        "protocol": "domestic",
        "base_url": "https://api.minimaxi.com/v1",
        "model": "MiniMax-M1",
        "description": "MiniMax OpenAI 兼容入口",
        "website": "https://www.minimaxi.com/",
    },
    {
        "id": "modelscope",
        "name": "ModelScope",
        "protocol": "domestic",
        "base_url": "https://api-inference.modelscope.cn/v1",
        "model": "Qwen/Qwen3-Coder-480B-A35B-Instruct",
        "description": "魔搭社区模型推理 OpenAI 兼容入口",
        "website": "https://modelscope.cn/",
    },
    {
        "id": "claude",
        "name": "Claude",
        "protocol": "claude",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-sonnet-4-20250514",
        "description": "Anthropic Claude 原生 Messages API",
        "website": "https://console.anthropic.com/",
    },
    {
        "id": "gemini",
        "name": "Gemini",
        "protocol": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-2.0-flash",
        "description": "Google Gemini 原生 API",
        "website": "https://aistudio.google.com/",
    },
]

VALID_PROTOCOLS = set(PROTOCOL_CATALOG)
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


def protocol_catalog() -> dict[str, dict[str, str]]:
    return {name: dict(info) for name, info in PROTOCOL_CATALOG.items()}


def provider_presets() -> list[dict[str, Any]]:
    return [dict(preset) for preset in PROVIDER_PRESETS]


def resolve_model(body: dict[str, Any], provider: dict[str, Any], default_model: str) -> str:
    """Resolve the effective model name applying aliases / provider override."""
    aliases = safe_model_aliases(provider)
    requested_model = body.get("model", "") if isinstance(body, dict) else ""
    if requested_model and requested_model in aliases:
        return aliases[requested_model]
    provider_model = safe_provider_model(provider)
    if provider_model:
        return provider_model
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
    aliases = safe_model_aliases(provider)
    requested_model = request_body.get("model", "")
    if requested_model and requested_model in aliases:
        request_body["model"] = aliases[requested_model]
    else:
        provider_model = safe_provider_model(provider)
        if provider_model:
            request_body["model"] = provider_model
        else:
            request_body.setdefault("model", default_model)
    return request_body


def build_forward_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def should_use_curl(provider: dict[str, Any]) -> bool:
    return safe_bool(provider.get("use_curl"), False)


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


def _model_list_request(provider: dict[str, Any], api_key: str) -> tuple[str, dict[str, str]]:
    protocol = provider_protocol(provider)
    base_url = provider["base_url"].rstrip("/")
    if protocol == "claude":
        return f"{base_url}/models", {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "Accept": "application/json",
        }
    if protocol == "gemini":
        return f"{base_url}/models", {
            "x-goog-api-key": api_key,
            "Accept": "application/json",
        }
    return f"{base_url}/models", {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def _normalize_models_response(provider: dict[str, Any], data: Any) -> list[dict[str, Any]]:
    protocol = provider_protocol(provider)
    if not isinstance(data, dict):
        return []

    if protocol == "gemini":
        models = data.get("models", [])
        normalized = []
        for model in models:
            if not isinstance(model, dict):
                continue
            raw_id = str(model.get("name") or model.get("id") or "").strip()
            if not raw_id:
                continue
            model_id = raw_id.removeprefix("models/")
            normalized.append({
                "id": model_id,
                "object": "model",
                "owned_by": provider.get("name", "gemini"),
            })
        return normalized

    models = data.get("data", [])
    normalized = []
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id") or model.get("name")
        if not model_id:
            continue
        item = dict(model)
        item["id"] = str(model_id).removeprefix("models/")
        item.setdefault("object", "model")
        normalized.append(item)
    return normalized


async def fetch_provider_models(provider: dict[str, Any]) -> list[dict[str, Any]]:
    keys = provider_api_keys(provider)
    if not keys:
        return []

    api_key = keys[0]
    url, headers = _model_list_request(provider, api_key)
    if should_use_curl(provider):
        curl_headers = []
        for key, value in headers.items():
            curl_headers += ["-H", f"{key}: {value}"]
        cmd = [_curl_bin(), "-s", "-S", "--max-time", "15", *curl_headers, url]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        data = json.loads(stdout.decode("utf-8", errors="replace"))
    else:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers)
        if response.status_code != 200:
            return []
        data = response.json()

    return _normalize_models_response(provider, data)
