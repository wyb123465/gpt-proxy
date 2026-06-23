from dataclasses import dataclass
from functools import cmp_to_key
import time
from typing import Any

from ._config import provider_api_keys, safe_bool, safe_provider_priority
from ._state import key_fingerprint


COOLDOWN_PRIORITY_PENALTY = 10_000
DEGRADED_PRIORITY_PENALTY = 1
DEGRADED_MIN_ATTEMPTS = 3
DEGRADED_SUCCESS_RATE = 0.5


@dataclass(frozen=True)
class RoutingCandidate:
    provider: dict[str, Any]
    reason: str = "primary"
    message: str = ""


@dataclass(frozen=True)
class ProviderRoutingProfile:
    provider: dict[str, Any]
    index: int
    priority: int
    all_keys_cooling: bool
    cooldown_seconds: int
    degraded: bool = False
    success_rate: float | None = None
    latency_samples: int = 0
    avg_latency_ms: float = 0.0

    @property
    def effective_priority(self) -> int:
        if self.all_keys_cooling:
            return self.priority + COOLDOWN_PRIORITY_PENALTY
        if self.degraded:
            return self.priority + DEGRADED_PRIORITY_PENALTY
        return self.priority


def _provider_name(provider: dict[str, Any]) -> str:
    return str(provider.get("name", "unknown") or "unknown")


def _provider_priority(provider: dict[str, Any]) -> int:
    return safe_provider_priority(provider)


def _active_key_cooldowns(provider: dict[str, Any], provider_state: dict[str, Any], now: float) -> list[float]:
    raw_cooldowns = provider_state.get("key_cooldowns")
    cooldowns = raw_cooldowns if isinstance(raw_cooldowns, dict) else {}
    active: list[float] = []
    for api_key in provider_api_keys(provider):
        try:
            cooldown_until = float(cooldowns.get(key_fingerprint(api_key), 0) or 0)
        except (TypeError, ValueError):
            continue
        if cooldown_until > now:
            active.append(cooldown_until)
    return active


def _provider_stats(provider: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    stats = state.get("_stats")
    if not isinstance(stats, dict):
        return {}
    providers = stats.get("providers")
    if not isinstance(providers, dict):
        return {}
    provider_stats = providers.get(_provider_name(provider))
    return provider_stats if isinstance(provider_stats, dict) else {}


def _provider_state(provider: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    provider_state = state.get(_provider_name(provider))
    return provider_state if isinstance(provider_state, dict) else {}


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


def _health_from_stats(stats: dict[str, Any]) -> tuple[bool, float | None, int, float]:
    attempts = _safe_int(stats.get("attempts"))
    success = _safe_int(stats.get("success"))
    latency_total = _safe_float(stats.get("latency_ms_total"))
    if attempts <= 0:
        return False, None, 0, 0.0
    success_rate = success / attempts
    avg_latency_ms = latency_total / attempts
    degraded = attempts >= DEGRADED_MIN_ATTEMPTS and success_rate < DEGRADED_SUCCESS_RATE
    return degraded, success_rate, attempts, avg_latency_ms


def build_provider_routing_profile(
    provider: dict[str, Any],
    state: dict[str, Any],
    index: int,
    now: float | None = None,
) -> ProviderRoutingProfile | None:
    if not safe_bool(provider.get("enabled"), True):
        return None
    keys = provider_api_keys(provider)
    if not keys:
        return None
    now = time.time() if now is None else now
    provider_state = _provider_state(provider, state)
    active_cooldowns = _active_key_cooldowns(provider, provider_state, now)
    all_keys_cooling = len(active_cooldowns) == len(keys) and bool(active_cooldowns)
    cooldown_seconds = max(0, int(max(active_cooldowns) - now)) if active_cooldowns else 0
    degraded, success_rate, latency_samples, avg_latency_ms = _health_from_stats(_provider_stats(provider, state))
    return ProviderRoutingProfile(
        provider=provider,
        index=index,
        priority=_provider_priority(provider),
        all_keys_cooling=all_keys_cooling,
        cooldown_seconds=cooldown_seconds,
        degraded=degraded,
        success_rate=success_rate,
        latency_samples=latency_samples,
        avg_latency_ms=avg_latency_ms,
    )


def _compare_profiles(left: ProviderRoutingProfile, right: ProviderRoutingProfile) -> int:
    left_key = (
        left.effective_priority,
        left.all_keys_cooling,
        left.degraded,
        left.priority,
    )
    right_key = (
        right.effective_priority,
        right.all_keys_cooling,
        right.degraded,
        right.priority,
    )
    if left_key != right_key:
        return -1 if left_key < right_key else 1

    if left.latency_samples >= DEGRADED_MIN_ATTEMPTS and right.latency_samples >= DEGRADED_MIN_ATTEMPTS:
        if left.avg_latency_ms != right.avg_latency_ms:
            return -1 if left.avg_latency_ms < right.avg_latency_ms else 1

    if left.index == right.index:
        return 0
    return -1 if left.index < right.index else 1


def order_providers_for_request(
    providers: list[dict[str, Any]],
    state: dict[str, Any],
    now: float | None = None,
) -> list[RoutingCandidate]:
    now = time.time() if now is None else now
    profiles = [
        profile
        for index, provider in enumerate(providers)
        if (profile := build_provider_routing_profile(provider, state, index, now)) is not None
    ]
    if not profiles:
        return []

    natural_first = sorted(profiles, key=lambda item: (item.priority, item.index))[0]
    ordered = sorted(profiles, key=cmp_to_key(_compare_profiles))

    candidates: list[RoutingCandidate] = []
    selected_first = ordered[0]
    for profile in ordered:
        reason = "primary"
        message = ""
        if profile is selected_first and profile.provider is not natural_first.provider:
            if natural_first.all_keys_cooling:
                reason = "cooldown_avoided"
                message = (
                    f"暂避 {_provider_name(natural_first.provider)} 的冷却 key，"
                    f"优先尝试 {_provider_name(profile.provider)}。"
                )
            elif natural_first.degraded:
                reason = "health_preferred"
                message = (
                    f"{_provider_name(natural_first.provider)} 最近成功率偏低，"
                    f"优先尝试 {_provider_name(profile.provider)}。"
                )
        candidates.append(RoutingCandidate(provider=profile.provider, reason=reason, message=message))
    return candidates
