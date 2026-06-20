import hashlib
import time
from datetime import datetime, timezone
from typing import Any


def key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def request_log_entry(
    provider_name: str,
    status: int | str,
    started_at: float,
    fallback_count: int,
    error: str | None = None,
    streamed: bool = False,
    path: str = "/v1/chat/completions",
    model: str | None = None,
    client_key: str | None = None,
) -> dict[str, Any]:
    entry = {
        "time": datetime.now(timezone.utc).isoformat(),
        "path": path,
        "provider": provider_name,
        "status": status,
        "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "fallback_count": fallback_count,
        "streamed": streamed,
        "error": error,
    }
    if model:
        entry["model"] = model
    if client_key:
        entry["client_key"] = client_key
    return entry


def _blank_counter() -> dict[str, Any]:
    return {
        "attempts": 0,
        "success": 0,
        "failed": 0,
        "streamed": 0,
        "fallbacks": 0,
        "latency_ms_total": 0.0,
    }


def _status_success(status: int | str) -> bool:
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        return False
    return 200 <= status_code < 400


def _bump_counter(counter: dict[str, Any], entry: dict[str, Any]) -> None:
    counter["attempts"] = int(counter.get("attempts", 0)) + 1
    if _status_success(entry.get("status")):
        counter["success"] = int(counter.get("success", 0)) + 1
    else:
        counter["failed"] = int(counter.get("failed", 0)) + 1
    if entry.get("streamed"):
        counter["streamed"] = int(counter.get("streamed", 0)) + 1
    counter["fallbacks"] = int(counter.get("fallbacks", 0)) + int(entry.get("fallback_count", 0) or 0)
    counter["latency_ms_total"] = float(counter.get("latency_ms_total", 0.0)) + float(entry.get("latency_ms", 0) or 0)


def record_request_stats(state: dict[str, Any], entry: dict[str, Any]) -> None:
    stats = state.setdefault(
        "_stats",
        {
            "total": _blank_counter(),
            "providers": {},
            "models": {},
            "paths": {},
            "client_keys": {},
        },
    )
    _bump_counter(stats.setdefault("total", _blank_counter()), entry)

    groups = {
        "providers": entry.get("provider") or "unknown",
        "models": entry.get("model") or "unknown",
        "paths": entry.get("path") or "unknown",
        "client_keys": entry.get("client_key") or "anonymous",
    }
    for group_name, item_name in groups.items():
        group = stats.setdefault(group_name, {})
        _bump_counter(group.setdefault(str(item_name), _blank_counter()), entry)


def _public_counter(name: str, counter: dict[str, Any]) -> dict[str, Any]:
    attempts = int(counter.get("attempts", 0) or 0)
    latency_total = float(counter.get("latency_ms_total", 0.0) or 0.0)
    return {
        "name": name,
        "attempts": attempts,
        "success": int(counter.get("success", 0) or 0),
        "failed": int(counter.get("failed", 0) or 0),
        "streamed": int(counter.get("streamed", 0) or 0),
        "fallbacks": int(counter.get("fallbacks", 0) or 0),
        "avg_latency_ms": round(latency_total / attempts, 2) if attempts else 0,
    }


def summarize_request_stats(state: dict[str, Any]) -> dict[str, Any]:
    stats = state.get("_stats") or {}
    total = _public_counter("total", stats.get("total") or _blank_counter())
    total.pop("name", None)

    def group_items(group_name: str) -> list[dict[str, Any]]:
        group = stats.get(group_name) or {}
        return sorted(
            [_public_counter(str(name), counter) for name, counter in group.items()],
            key=lambda item: (-item["attempts"], item["name"]),
        )

    return {
        "total": total,
        "providers": group_items("providers"),
        "models": group_items("models"),
        "paths": group_items("paths"),
        "client_keys": group_items("client_keys"),
    }
