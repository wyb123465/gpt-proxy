import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ._config import provider_api_keys
from ._providers import (
    _prepare_forward,
    curl_forward_to_provider,
    curl_stream_request,
    forward_to_provider,
    provider_protocol,
    safe_response_detail,
    should_use_curl,
)
from ._routing import order_providers_for_request
from ._state import request_log_entry


RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
logger = logging.getLogger("gpt_proxy")


@dataclass(frozen=True)
class ProxyServices:
    create_http_client: Callable[[], httpx.AsyncClient]
    get_http_client: Callable[[Any], httpx.AsyncClient]
    load_state: Callable[[], dict[str, Any]]
    append_request_log: Callable[[dict[str, Any]], None]
    key_attempt_order: Callable[[dict[str, Any], dict[str, Any]], list[tuple[int, str]]]
    record_key_cooldown: Callable[[str, str], None]
    record_success: Callable[[str, httpx.Response, int, int | None], None]
    route_decision_entry: Callable[..., dict[str, Any]]


async def stream_response_bytes(
    response: httpx.Response,
    services: ProxyServices,
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
            services.append_request_log(log_entry)
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
        status_code, data = await curl_forward_to_provider(
            body, provider, default_model, api_key,
            protocol=protocol, path_suffix=path_suffix, passthrough=passthrough,
        )
        if status_code == 200:
            return ("curl", data)
        if status_code in RETRYABLE_STATUS_CODES:
            detail = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
            return ("retryable", {"status": status_code, "detail": detail})
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


def request_model_for_log(body: dict[str, Any], path: str, default_model: str) -> str:
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
    services: ProxyServices,
    callback: Any,
    stream: bool,
    path: str = "/v1/chat/completions",
    fwd: dict[str, Any] | None = None,
    log_model: str | None = None,
    client_key_label: str | None = None,
) -> Any:
    last_error = "所有后端 API 均不可用"
    fallback_count = 0
    state = services.load_state()
    log_model = log_model or request_model_for_log(body, path, config.get("default_model", "gpt-3.5-turbo"))
    previous_provider: str | None = None
    previous_status: int | str | None = None

    for routing_candidate in order_providers_for_request(config["providers"], state):
        provider = routing_candidate.provider
        provider_name = provider["name"]
        key_attempts = services.key_attempt_order(provider, state)
        key_count = len(provider_api_keys(provider))
        for key_index, api_key in key_attempts:
            started_at = time.perf_counter()
            route_decision = services.route_decision_entry(
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
                services.append_request_log(request_log_entry(provider_name, "request_error", started_at, fallback_count, last_error, streamed=stream, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision))
                previous_provider = provider_name
                previous_status = "request_error"
                fallback_count += 1
                continue
            except HTTPException as exc:
                logger.info("provider=%s status=%s path=%s", provider_name, exc.status_code, path)
                services.append_request_log(request_log_entry(provider_name, exc.status_code, started_at, fallback_count, streamed=stream, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision))
                raise

            if kind == "retryable":
                retry_info = data
                logger.info("provider=%s status=%s path=%s", provider_name, retry_info["status"], path)
                services.append_request_log(request_log_entry(provider_name, retry_info["status"], started_at, fallback_count, retry_info["detail"], streamed=stream, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision))
                if retry_info["status"] == 429:
                    services.record_key_cooldown(provider_name, api_key)
                last_error = retry_info["detail"]
                previous_provider = provider_name
                previous_status = retry_info["status"]
                fallback_count += 1
                continue

            if kind == "fatal_response":
                return data

            logger.info("provider=%s status=200 path=%s", provider_name, path)
            if kind == "response":
                services.record_success(provider_name, data, key_count, key_index)
            else:
                services.record_success(provider_name, httpx.Response(200, json={}), key_count, key_index)
            return callback(data, provider_name, started_at, fallback_count, key_attempts, key_index, client, stream, path, log_model, client_key_label, route_decision, services)

    raise HTTPException(status_code=502, detail=last_error)


def stream_callback(
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
    services: ProxyServices,
) -> StreamingResponse:
    if isinstance(data, httpx.Response):
        log_entry = request_log_entry(provider_name, data.status_code, started_at, fallback_count, streamed=True, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision)
        return StreamingResponse(
            stream_response_bytes(data, services, log_entry),
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
            services.append_request_log(log_entry)

    if hasattr(data, "__aiter__"):
        log_entry = request_log_entry(provider_name, 200, started_at, fallback_count, streamed=True, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision)
        return StreamingResponse(_curl_stream_gen(data, log_entry), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    log_entry = request_log_entry(provider_name, 200, started_at, fallback_count, streamed=True, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision)
    log_entry["stream_status"] = "stream_complete"
    services.append_request_log(log_entry)
    payload = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
    return StreamingResponse(iter([payload.encode("utf-8")]), media_type="text/event-stream")


def json_callback(
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
    services: ProxyServices,
) -> JSONResponse:
    services.append_request_log(request_log_entry(provider_name, 200, started_at, fallback_count, path=path, model=log_model, client_key=client_key_label, route_decision=route_decision))
    content = data.json() if isinstance(data, httpx.Response) else data
    return JSONResponse(content=content, status_code=200)


async def stream_chat_completions(
    body: dict[str, Any],
    config: dict[str, Any],
    services: ProxyServices,
    request: Request | None = None,
    path: str = "/v1/chat/completions",
    fwd: dict[str, Any] | None = None,
) -> StreamingResponse:
    client = services.get_http_client(request.app) if request else services.create_http_client()
    client_key_label = getattr(request.state, "client_key_label", None) if request else None
    return await _iterate_providers(
        body, config, client, services, stream_callback,
        stream=True, path=path, fwd=fwd,
        log_model=request_model_for_log(body, path, config.get("default_model", "gpt-3.5-turbo")),
        client_key_label=client_key_label,
    )


async def proxy_chat_json(
    body: dict[str, Any],
    config: dict[str, Any],
    services: ProxyServices,
    request: Request | None = None,
    path: str = "/v1/chat/completions",
    fwd: dict[str, Any] | None = None,
) -> JSONResponse:
    client = services.get_http_client(request.app) if request else services.create_http_client()
    client_key_label = getattr(request.state, "client_key_label", None) if request else None
    return await _iterate_providers(
        body, config, client, services, json_callback,
        stream=False, path=path, fwd=fwd,
        log_model=request_model_for_log(body, path, config.get("default_model", "gpt-3.5-turbo")),
        client_key_label=client_key_label,
    )


def filter_config_by_protocol(config: dict[str, Any], protocols: set[str]) -> dict[str, Any]:
    providers = [provider for provider in config["providers"] if provider_protocol(provider) in protocols]
    return {"providers": providers, "default_model": config.get("default_model", "gpt-3.5-turbo")}


async def passthrough(
    body: dict[str, Any],
    config: dict[str, Any],
    request: Request,
    services: ProxyServices,
    protocol: str,
    path_suffix: str,
    inbound_path: str,
) -> Any:
    fwd = {"protocol": protocol, "path_suffix": path_suffix}
    if body.get("stream") is True:
        return await stream_chat_completions(body, config, services, request=request, path=inbound_path, fwd=fwd)
    return await proxy_chat_json(body, config, services, request=request, path=inbound_path, fwd=fwd)
