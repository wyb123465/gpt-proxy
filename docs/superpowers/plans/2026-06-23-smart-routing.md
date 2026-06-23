# Smart Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add lightweight health-aware provider ordering while preserving `gpt-proxy`'s local, deterministic, priority-first routing model.

**Current status:** Implemented and verified on this branch. The final diff is broader than the original routing-only plan because the same hardening pass also added observability, redacted config export, model coverage, auth-mode visibility, and defensive handling for hand-edited config/state shapes.

**Architecture:** Add a focused pure routing helper module that scores already-filtered providers using existing `state.json` cooldown and stats data. Wire `_iterate_providers()` to use the ordered candidates and pass smart-routing explanations into the existing `route_decision` log path.

**Tech Stack:** Python 3.12, FastAPI, httpx, pytest, existing JSON config/state files, vanilla dashboard JavaScript.

---

## File Structure

- Create `main/_routing.py`: Pure routing helpers. Owns provider cooldown detection, degraded-health detection, deterministic ordering, and smart-route explanation objects.
- Modify `main/__init__.py`: Import routing helpers, call them inside `_iterate_providers()`, and let `route_decision_entry()` use smart-route notes when the first selected provider differs from natural priority order.
- Modify `tests/test_proxy.py`: Add request-level behavior tests for cooldown avoidance, health preference, priority preservation, and route-decision explanations.
- Modify `README.md`: Mention lightweight smart routing in the supported capabilities list after tests pass.

## Task 1: Cooldown Avoidance Test

**Files:**
- Modify: `tests/test_proxy.py`

- [x] **Step 1: Write the failing test**

Add this test near the existing `test_429_cools_down_key_for_next_request` and route-decision tests:

```python
def test_smart_routing_avoids_provider_when_all_keys_are_cooling(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("official", 0, api_key="hot-key"),
            make_provider("backup", 1, api_key="backup-key"),
        ],
    )
    state_path.write_text(
        json.dumps(
            {
                "official": {
                    "key_cooldowns": {
                        main.key_fingerprint("hot-key"): main.time.time() + 60,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})

    assert response.status_code == 200
    assert len(calls) == 1
    assert "backup.example" in calls[0]
    entry = client.get("/api/requests").json()["requests"][0]
    assert entry["provider"] == "backup"
    assert entry["route_decision"]["reason"] == "cooldown_avoided"
    assert "official" in entry["route_decision"]["message"]
```

- [x] **Step 2: Run test to verify it fails**

Run:

```powershell
uv run python -m pytest tests/test_proxy.py::test_smart_routing_avoids_provider_when_all_keys_are_cooling -q -p no:cacheprovider
```

Expected: FAIL because the current routing loop tries `official` first and logs `primary`.

## Task 2: Cooldown-Aware Routing Helper

**Files:**
- Create: `main/_routing.py`
- Modify: `main/__init__.py`

- [x] **Step 1: Implement the pure helper**

Create `main/_routing.py`:

```python
from dataclasses import dataclass
import time
from typing import Any

from ._config import provider_api_keys
from ._state import key_fingerprint


COOLDOWN_PRIORITY_PENALTY = 10_000


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
    avg_latency_ms: float = 0.0

    @property
    def effective_priority(self) -> int:
        if self.all_keys_cooling:
            return self.priority + COOLDOWN_PRIORITY_PENALTY
        return self.priority


def _provider_name(provider: dict[str, Any]) -> str:
    return str(provider.get("name", "unknown") or "unknown")


def _provider_priority(provider: dict[str, Any]) -> int:
    try:
        return int(provider.get("priority", 1000))
    except (TypeError, ValueError):
        return 1000


def _active_key_cooldowns(provider: dict[str, Any], provider_state: dict[str, Any], now: float) -> list[float]:
    cooldowns = provider_state.get("key_cooldowns") or {}
    active: list[float] = []
    for api_key in provider_api_keys(provider):
        try:
            cooldown_until = float(cooldowns.get(key_fingerprint(api_key), 0) or 0)
        except (TypeError, ValueError):
            continue
        if cooldown_until > now:
            active.append(cooldown_until)
    return active


def build_provider_routing_profile(
    provider: dict[str, Any],
    state: dict[str, Any],
    index: int,
    now: float | None = None,
) -> ProviderRoutingProfile | None:
    if not provider.get("enabled", True):
        return None
    if not provider_api_keys(provider):
        return None
    now = time.time() if now is None else now
    provider_state = state.get(_provider_name(provider), {}) or {}
    active_cooldowns = _active_key_cooldowns(provider, provider_state, now)
    all_keys_cooling = len(active_cooldowns) == len(provider_api_keys(provider)) and bool(active_cooldowns)
    cooldown_seconds = max(0, int(max(active_cooldowns) - now)) if active_cooldowns else 0
    return ProviderRoutingProfile(
        provider=provider,
        index=index,
        priority=_provider_priority(provider),
        all_keys_cooling=all_keys_cooling,
        cooldown_seconds=cooldown_seconds,
    )


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
    ordered = sorted(
        profiles,
        key=lambda item: (
            item.effective_priority,
            item.all_keys_cooling,
            item.priority,
            item.index,
        ),
    )

    candidates: list[RoutingCandidate] = []
    selected_first = ordered[0]
    for profile in ordered:
        reason = "primary"
        message = ""
        if profile is selected_first and profile.provider is not natural_first.provider and natural_first.all_keys_cooling:
            reason = "cooldown_avoided"
            message = (
                f"暂避 { _provider_name(natural_first.provider) } 的冷却 key，"
                f"优先尝试 { _provider_name(profile.provider) }。"
            )
        candidates.append(RoutingCandidate(provider=profile.provider, reason=reason, message=message))
    return candidates
```

- [x] **Step 2: Wire helper into the routing loop**

In `main/__init__.py`, add the import:

```python
from ._routing import RoutingCandidate, order_providers_for_request
```

Change `route_decision_entry()` signature:

```python
def route_decision_entry(
    provider: dict[str, Any],
    fallback_count: int,
    key_index: int,
    key_count: int,
    previous_provider: str | None = None,
    previous_status: int | str | None = None,
    routing_candidate: RoutingCandidate | None = None,
) -> dict[str, Any]:
```

Inside `route_decision_entry()`, before the existing `if fallback_count <= 0:` branch, add:

```python
    if fallback_count <= 0 and routing_candidate and routing_candidate.reason != "primary":
        reason = routing_candidate.reason
        message = routing_candidate.message or f"智能路由优先尝试 {provider_name}。"
```

Then change the existing `if fallback_count <= 0:` to `elif fallback_count <= 0:`.

In `_iterate_providers()`, replace:

```python
    for provider in config["providers"]:
```

with:

```python
    for routing_candidate in order_providers_for_request(config["providers"], state):
        provider = routing_candidate.provider
```

Pass the candidate into `route_decision_entry()`:

```python
                routing_candidate=routing_candidate,
```

- [x] **Step 3: Run test to verify it passes**

Run:

```powershell
uv run python -m pytest tests/test_proxy.py::test_smart_routing_avoids_provider_when_all_keys_are_cooling -q -p no:cacheprovider
```

Expected: PASS.

## Task 3: Health Preference Test

**Files:**
- Modify: `tests/test_proxy.py`

- [x] **Step 1: Write the failing test**

Add:

```python
def test_smart_routing_prefers_healthier_provider_at_same_priority(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("shaky", 0),
            make_provider("steady", 0),
        ],
    )
    state_path.write_text(
        json.dumps(
            {
                "_stats": {
                    "providers": {
                        "shaky": {
                            "attempts": 4,
                            "success": 1,
                            "failed": 3,
                            "streamed": 0,
                            "fallbacks": 3,
                            "latency_ms_total": 800,
                        },
                        "steady": {
                            "attempts": 4,
                            "success": 4,
                            "failed": 0,
                            "streamed": 0,
                            "fallbacks": 0,
                            "latency_ms_total": 400,
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})

    assert response.status_code == 200
    assert len(calls) == 1
    assert "steady.example" in calls[0]
    entry = client.get("/api/requests").json()["requests"][0]
    assert entry["provider"] == "steady"
    assert entry["route_decision"]["reason"] == "health_preferred"
    assert "shaky" in entry["route_decision"]["message"]
```

- [x] **Step 2: Run test to verify it fails**

Run:

```powershell
uv run python -m pytest tests/test_proxy.py::test_smart_routing_prefers_healthier_provider_at_same_priority -q -p no:cacheprovider
```

Expected: FAIL because Task 2 only handles cooldown avoidance.

## Task 4: Degraded-Health Ordering

**Files:**
- Modify: `main/_routing.py`

- [x] **Step 1: Add degraded-health scoring**

In `main/_routing.py`, add constants:

```python
DEGRADED_PRIORITY_PENALTY = 1
DEGRADED_MIN_ATTEMPTS = 3
DEGRADED_SUCCESS_RATE = 0.5
```

Add:

```python
def _provider_stats(provider: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return ((state.get("_stats") or {}).get("providers") or {}).get(_provider_name(provider)) or {}


def _health_from_stats(stats: dict[str, Any]) -> tuple[bool, float | None, float]:
    attempts = int(stats.get("attempts", 0) or 0)
    success = int(stats.get("success", 0) or 0)
    latency_total = float(stats.get("latency_ms_total", 0.0) or 0.0)
    if attempts <= 0:
        return False, None, 0.0
    success_rate = success / attempts
    avg_latency_ms = latency_total / attempts
    degraded = attempts >= DEGRADED_MIN_ATTEMPTS and success_rate < DEGRADED_SUCCESS_RATE
    return degraded, success_rate, avg_latency_ms
```

Update `ProviderRoutingProfile.effective_priority`:

```python
    @property
    def effective_priority(self) -> int:
        if self.all_keys_cooling:
            return self.priority + COOLDOWN_PRIORITY_PENALTY
        if self.degraded:
            return self.priority + DEGRADED_PRIORITY_PENALTY
        return self.priority
```

Inside `build_provider_routing_profile()`, before returning:

```python
    stats = _provider_stats(provider, state)
    degraded, success_rate, avg_latency_ms = _health_from_stats(stats)
```

Set the return fields:

```python
        degraded=degraded,
        success_rate=success_rate,
        avg_latency_ms=avg_latency_ms,
```

Update the sort key:

```python
        key=lambda item: (
            item.effective_priority,
            item.all_keys_cooling,
            item.degraded,
            item.avg_latency_ms,
            item.priority,
            item.index,
        ),
```

Update smart reason selection:

```python
        if profile is selected_first and profile.provider is not natural_first.provider:
            if natural_first.all_keys_cooling:
                reason = "cooldown_avoided"
                message = (
                    f"暂避 { _provider_name(natural_first.provider) } 的冷却 key，"
                    f"优先尝试 { _provider_name(profile.provider) }。"
                )
            elif natural_first.degraded:
                reason = "health_preferred"
                message = (
                    f"{ _provider_name(natural_first.provider) } 最近成功率偏低，"
                    f"优先尝试 { _provider_name(profile.provider) }。"
                )
```

- [x] **Step 2: Run test to verify it passes**

Run:

```powershell
uv run python -m pytest tests/test_proxy.py::test_smart_routing_prefers_healthier_provider_at_same_priority -q -p no:cacheprovider
```

Expected: PASS.

## Task 5: Priority Preservation Test

**Files:**
- Modify: `tests/test_proxy.py`

- [x] **Step 1: Write the test**

Add:

```python
def test_smart_routing_keeps_healthy_higher_priority_provider_first(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    state_path = tmp_path / "state.json"
    write_config(
        config_path,
        [
            make_provider("official", 0),
            make_provider("backup", 1),
        ],
    )
    state_path.write_text(
        json.dumps(
            {
                "_stats": {
                    "providers": {
                        "official": {
                            "attempts": 3,
                            "success": 3,
                            "failed": 0,
                            "streamed": 0,
                            "fallbacks": 0,
                            "latency_ms_total": 300,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "CONFIG_PATH", config_path)
    monkeypatch.setattr(main, "STATE_PATH", state_path)

    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(
        main,
        "create_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0),
    )

    client = TestClient(main.app)
    response = client.post("/v1/chat/completions", json={"messages": []})

    assert response.status_code == 200
    assert len(calls) == 1
    assert "official.example" in calls[0]
    entry = client.get("/api/requests").json()["requests"][0]
    assert entry["provider"] == "official"
    assert entry["route_decision"]["reason"] == "primary"
```

- [x] **Step 2: Run test to verify it passes**

Run:

```powershell
uv run python -m pytest tests/test_proxy.py::test_smart_routing_keeps_healthy_higher_priority_provider_first -q -p no:cacheprovider
```

Expected: PASS. If it fails, adjust sorting so healthy priority `0` stays before priority `1`.

## Task 6: Existing Routing Regression Sweep

**Files:**
- Modify only if a regression appears: `main/_routing.py`, `main/__init__.py`, `tests/test_proxy.py`

- [x] **Step 1: Run focused routing tests**

Run:

```powershell
uv run python -m pytest tests/test_proxy.py::test_fallback_after_quota_error tests/test_proxy.py::test_429_cools_down_key_for_next_request tests/test_proxy.py::test_request_log_records_route_decision_after_fallback tests/test_proxy.py::test_smart_routing_avoids_provider_when_all_keys_are_cooling tests/test_proxy.py::test_smart_routing_prefers_healthier_provider_at_same_priority tests/test_proxy.py::test_smart_routing_keeps_healthy_higher_priority_provider_first -q -p no:cacheprovider
```

Expected: PASS for all selected tests.

- [x] **Step 2: Fix regressions if any selected test fails**

Use the failing assertion to update only the routing helper or the route-decision wiring. Preserve existing fallback semantics:

```python
elif previous_provider == provider_name:
    reason = "key_retry"
    message = f"{provider_name} 的上一个 key 返回 {previous_status}，自动尝试同 provider 的下一个 key。"
else:
    reason = "fallback"
    previous = previous_provider or "上一个 provider"
    message = f"{previous} 返回 {previous_status} 后，按优先级回退到 {provider_name}。"
```

## Task 7: README Update

**Files:**
- Modify: `README.md`

- [x] **Step 1: Update capability list**

In `README.md`, change the automatic fallback bullet to:

```markdown
- **轻量智能路由**：保留优先级配置，同时根据 429 冷却、成功率和平均耗时暂避不稳定 provider
```

Keep the existing multi-key rotation and request-log bullets.

- [x] **Step 2: Run README diff check**

Run:

```powershell
git diff -- README.md
```

Expected: README mentions lightweight smart routing without claiming cost-aware routing or server-style account management.

## Task 8: Full Verification

**Files:**
- No planned edits.

- [x] **Step 1: Run Python test suite**

Run:

```powershell
uv run python -m pytest -q -p no:cacheprovider
```

Expected: all tests pass.

- [x] **Step 2: Run dashboard JavaScript syntax check**

Run:

```powershell
node --check static\app.js
```

Expected: no output and exit code `0`.

- [x] **Step 3: Inspect final diff**

Run:

```powershell
git diff --stat
```

Expected: changes are limited to routing helper, routing loop/tests, README, and the new docs under `docs/superpowers/`.
