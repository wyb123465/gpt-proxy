import fnmatch
import hmac
import time
from typing import Any

from fastapi import HTTPException, Request

from ._state import key_fingerprint


def request_token(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    x_api_key = request.headers.get("x-api-key", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return x_api_key.strip()


def secret_matches(provided: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(provided, expected)


def require_proxy_access_token(request: Request, proxy_token: str) -> None:
    if not proxy_token:
        return
    if secret_matches(request_token(request), proxy_token):
        return
    raise HTTPException(status_code=401, detail="Missing or invalid local proxy access token")


def management_auth_mode(proxy_token: str) -> str:
    return "proxy_token" if proxy_token else "open"


def v1_auth_mode(proxy_token: str, has_client_keys: bool) -> str:
    if proxy_token and has_client_keys:
        return "proxy_token_or_client_keys"
    if proxy_token:
        return "proxy_token"
    if has_client_keys:
        return "client_keys"
    return "open"


def model_matches_any(model: str, patterns: list[str]) -> bool:
    normalized = model.strip().lower()
    return any(fnmatch.fnmatchcase(normalized, pattern.strip().lower()) for pattern in patterns if pattern.strip())


def model_allowed_for_rules(model: str | None, allowed_models: list[str], excluded_models: list[str]) -> bool:
    if not model:
        return True
    if allowed_models and not model_matches_any(model, allowed_models):
        return False
    if excluded_models and model_matches_any(model, excluded_models):
        return False
    return True


def authorize_model_for_rules(model: str | None, allowed_models: list[str], excluded_models: list[str]) -> None:
    if not model:
        return
    if allowed_models and not model_matches_any(model, allowed_models):
        raise HTTPException(status_code=403, detail=f"Model '{model}' is not allowed for this local client key")
    if excluded_models and model_matches_any(model, excluded_models):
        raise HTTPException(status_code=403, detail=f"Model '{model}' is excluded for this local client key")


def rate_limit_identity(request: Request) -> str:
    token = request_token(request)
    if token:
        return f"token:{key_fingerprint(token)}"
    client_host = request.client.host if request.client else "local"
    return f"host:{client_host}"


def enforce_rate_limit(
    request: Request,
    buckets: dict[str, list[float]],
    limit_per_minute: int,
    now: float | None = None,
) -> None:
    if limit_per_minute <= 0:
        return
    identity = rate_limit_identity(request)
    now = time.time() if now is None else now
    window_start = now - 60

    stale_threshold = now - 120
    stale_keys = [key for key, values in buckets.items() if not values or values[-1] < stale_threshold]
    for key in stale_keys:
        del buckets[key]

    bucket = [timestamp for timestamp in buckets.get(identity, []) if timestamp > window_start]
    if len(bucket) >= limit_per_minute:
        buckets[identity] = bucket
        raise HTTPException(status_code=429, detail="Local proxy rate limit exceeded")
    bucket.append(now)
    buckets[identity] = bucket
