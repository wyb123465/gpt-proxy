# Smart Routing Design

## Context

`gpt-proxy` is a lightweight local API proxy. It already supports provider priority, multi-key rotation, retryable fallback, 429 key cooldowns, request logs, route-decision messages, provider health summaries, and model coverage reporting.

The next improvement should make routing more reliable without turning the project into a heavy server-side platform. The feature should preserve the user's configured priority as the main routing intent while using recent local health data to avoid obviously weak choices.

## Goals

- Prefer healthy providers when multiple configured providers can handle the same request.
- Avoid providers whose keys are all still cooling down when other usable providers exist.
- Keep configured `priority` as the main ordering signal.
- Explain smart-routing choices in request logs so the UI can show why a provider was selected.
- Keep the implementation local and file-backed, using the existing `state.json` counters.

## Non-Goals

- No cost-aware routing, balance tracking, account management, or database schema.
- No cross-protocol conversion changes.
- No new background scheduler.
- No random weighted distribution. Routing should stay deterministic and easy to debug.

## Candidate Ordering

Before `_iterate_providers()` attempts a request, it should build an ordered candidate list from the already protocol-filtered config.

The candidate list should:

- Exclude disabled providers and providers without usable keys.
- Preserve ascending `priority` as the strongest signal.
- Treat providers with all keys in active cooldown as temporarily avoidable.
- Use recent provider stats from `state["_stats"]["providers"]` to identify degraded providers.
- Prefer healthier providers within the same or near priority band.

A provider is "degraded" when it has enough samples and a materially worse recent record than peers. The initial rule should stay conservative:

- Fewer than 3 attempts means insufficient history and should not penalize the provider.
- Success rate below 50% after at least 3 attempts is degraded.
- Average latency may be used as a tie-breaker, not as a hard block.
- Cooldown is stronger than degraded health because it usually means immediate quota/rate-limit pressure.

Priority still wins across distant bands. A lower-priority provider should only move ahead when the higher-priority provider is cooling down or clearly degraded, or when both are in the same priority value.

## Route Decisions

`route_decision` should keep the current fields and add a compact machine-readable `routing_reason` or reuse `reason` with new values when the smart-routing layer changes the natural priority order.

Suggested reasons:

- `primary`: normal first choice by priority.
- `health_preferred`: selected because a peer in the same/near priority band had worse health.
- `cooldown_avoided`: selected because a higher-priority peer had all keys cooling down.
- `fallback`: selected after an attempted provider failed.
- `key_retry`: selected another key on the same provider after a retryable failure.

The human-readable `message` should mention the provider or condition that influenced the choice, without exposing API keys.

## UI Impact

No major UI redesign is required for this slice. The existing request log already renders `route_decision.message`, and the provider table already shows health/cooldown. The only UI work needed is small copy compatibility if new reason labels appear.

## Testing

Add behavior tests before implementation:

- A provider whose only key is cooling down is attempted after a usable peer, even if it has a better priority.
- A same-priority provider with a poor success rate is ordered after a healthier peer.
- A healthy higher-priority provider still wins over a lower-priority provider.
- Request logs include a smart-routing reason when ordering changes because of cooldown or health.
- Existing fallback and key-rotation tests continue to pass.

## Risks

- Over-aggressive health penalties could surprise users. Mitigation: keep thresholds conservative and priority-first.
- Stale stats could punish a recovered provider. Mitigation: cooldown is time-bound, and health penalties only reorder, not permanently disable.
- UI copy could imply a provider was skipped when it was only deferred. Mitigation: use wording like "优先尝试" and "暂避".

## Acceptance Criteria

- Requests still work with the current config format.
- Provider ordering is deterministic.
- Smart routing does not bypass protocol filtering.
- Tests cover cooldown avoidance, health preference, priority preservation, and log explanations.
- `uv run python -m pytest -q -p no:cacheprovider` passes.
- `node --check static\app.js` passes if UI JavaScript changes.
