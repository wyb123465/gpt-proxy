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
