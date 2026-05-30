import asyncio
import json
import shutil
from typing import Any, AsyncIterator

import httpx

from ._config import provider_api_keys


def _curl_bin() -> str:
    return shutil.which("curl") or "curl"


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


async def fetch_provider_models(provider: dict[str, Any]) -> list[dict[str, Any]]:
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
