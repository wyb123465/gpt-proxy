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
    route_decision: dict[str, Any] | None = None,
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
    if route_decision:
        entry["route_decision"] = route_decision
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


def _blank_stats() -> dict[str, Any]:
    return {
        "total": _blank_counter(),
        "providers": {},
        "models": {},
        "paths": {},
        "client_keys": {},
    }


def stats_container(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        return {}
    stats = state.get("_stats")
    return stats if isinstance(stats, dict) else {}


def _ensure_stats_container(state: dict[str, Any]) -> dict[str, Any]:
    stats = state.get("_stats")
    if not isinstance(stats, dict):
        stats = _blank_stats()
        state["_stats"] = stats
        return stats
    stats.setdefault("total", _blank_counter())
    for group_name in ("providers", "models", "paths", "client_keys"):
        if not isinstance(stats.get(group_name), dict):
            stats[group_name] = {}
    return stats


def _status_success(status: int | str) -> bool:
    try:
        status_code = int(status)
    except (TypeError, ValueError):
        return False
    return 200 <= status_code < 400


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _bump_counter(counter: dict[str, Any], entry: dict[str, Any]) -> None:
    counter["attempts"] = _safe_int(counter.get("attempts")) + 1
    if _status_success(entry.get("status")):
        counter["success"] = _safe_int(counter.get("success")) + 1
    else:
        counter["failed"] = _safe_int(counter.get("failed")) + 1
    if entry.get("streamed"):
        counter["streamed"] = _safe_int(counter.get("streamed")) + 1
    counter["fallbacks"] = _safe_int(counter.get("fallbacks")) + _safe_int(entry.get("fallback_count"))
    counter["latency_ms_total"] = _safe_float(counter.get("latency_ms_total")) + _safe_float(entry.get("latency_ms"))


def record_request_stats(state: dict[str, Any], entry: dict[str, Any]) -> None:
    stats = _ensure_stats_container(state)
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
    attempts = _safe_int(counter.get("attempts"))
    latency_total = _safe_float(counter.get("latency_ms_total"))
    return {
        "name": name,
        "attempts": attempts,
        "success": _safe_int(counter.get("success")),
        "failed": _safe_int(counter.get("failed")),
        "streamed": _safe_int(counter.get("streamed")),
        "fallbacks": _safe_int(counter.get("fallbacks")),
        "avg_latency_ms": round(latency_total / attempts, 2) if attempts else 0,
    }


def summarize_request_stats(state: dict[str, Any]) -> dict[str, Any]:
    stats = stats_container(state)
    total_counter = stats.get("total")
    total = _public_counter("total", total_counter if isinstance(total_counter, dict) else _blank_counter())
    total.pop("name", None)

    def group_items(group_name: str) -> list[dict[str, Any]]:
        group = stats.get(group_name) or {}
        if not isinstance(group, dict):
            return []
        return sorted(
            [_public_counter(str(name), counter) for name, counter in group.items() if isinstance(counter, dict)],
            key=lambda item: (-item["attempts"], item["name"]),
        )

    return {
        "total": total,
        "providers": group_items("providers"),
        "models": group_items("models"),
        "paths": group_items("paths"),
        "client_keys": group_items("client_keys"),
    }
