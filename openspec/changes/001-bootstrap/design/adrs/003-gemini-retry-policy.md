# ADR 003: Gemini retry policy for the embedding adapter

- **Status**: Accepted
- **Date**: 2026-08-05
- **Change**: `001-bootstrap`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

The `GeminiEmbeddingAdapter` (in `src/mcp_server/infrastructure/adapters/gemini_embedding.py`) calls Google Gemini's `text-embedding-004` model from the preindex pipeline at build time, and the runtime chat tool at request time. The spec says: "exponential backoff up to 3 attempts" and "on final failure MUST exit with `PreindexExitCode.GEMINI_ERROR`".

The preindex pipeline hits Gemini potentially thousands of times in a row during a full portfolio rebuild. Gemini's free tier enforces **15 RPM** for Flash and **1500 RPM** for `text-embedding-004`; we additionally sleep 0.1 s between calls (spec). Real failures to handle: HTTP 429 (rate limit), HTTP 5xx (server errors), transient network timeouts. We must NOT retry on 4xx other than 429 (those are user errors — bad API key, malformed payload, model deprecated) — retrying wastes quota and burns the recruiter-demo machine's clock.

## Decision Drivers

- **D1**: Free-tier quota safety — never pound the API with retries.
- **D2**: Determinism — same input produces same retry behavior across CI and local dev.
- **D3**: Testability — the retry policy must be unit-testable with `httpx.MockTransport` or `unittest.mock`.
- **D4**: Spec compliance — exactly 3 attempts.
- **D5**: Build-time vs runtime behavior is identical — same adapter, same retry policy.

## Considered Options

### Option A — Hand-rolled decorator with exponential backoff + jitter (chosen)

```python
@retry(max_attempts=3, base_delay=1.0, max_delay=30.0,
       jitter="full", retry_on=(429, 500, 502, 503, 504))
def _call_gemini_embed(self, text: str) -> list[float]: ...
```

`tenacity` would do this too, but adds a dep for ~30 lines of policy. The 5-line implementation is clear:

```python
def retry_with_backoff(fn, *, max_attempts, base_delay, max_delay,
                       jitter, retry_on):
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except GeminiTransientError as e:
            if attempt == max_attempts: raise
            delay = compute_delay(attempt, base_delay, max_delay, jitter)
            time.sleep(delay)
```

### Option B — `tenacity` library (rejected)

Pros: well-tested, batteries-included. Cons: 50 KB dep, magical decorator semantics, harder to mock in unit tests (`tenacity` swallows test-time sleeps unless you `tenacity.stop_after_attempt` + `time.sleep` patches). For a 30-line policy, hand-rolled wins.

### Option C — No retry (rejected)

A single transient 503 means preindex aborts with `GEMINI_ERROR`. Operationally fragile.

### Option D — Circuit breaker (deferred)

A circuit breaker (open after N consecutive failures, half-open after cooldown) is nice for runtime, but **preindex is one-shot** — when it fails, the operator restarts it. A circuit breaker adds complexity that doesn't pay off in a one-shot build context. Defer to `002-mcp-tools` (runtime search) if at all.

## Decision

**Option A — hand-rolled retry with exponential backoff + full jitter.**

### Concrete policy

| Knob | Value | Rationale |
|---|---|---|
| `max_attempts` | **3** | Spec mandate |
| `base_delay` | **1.0 s** | 1s, 2s, 4s — bounded by max_delay below |
| `max_delay` | **30.0 s** | Cap any single backoff |
| `jitter` | **"full"** | Randomize `delay ∈ [0, computed]` to avoid thundering-herd if many adapters retry simultaneously |
| `retry_on` | **HTTP 429, 500, 502, 503, 504** | Transient only |
| `fail_fast_on` | **400, 401, 403, 404** | Bad API key, model not found, payload rejected — do not retry |

`GeminiTransientError` is raised by the SDK wrapper when the HTTP status is in `retry_on` OR when `httpx.ConnectError` / `httpx.TimeoutException` fires.

### Jitter math

`computed_delay(attempt) = min(max_delay, base_delay * 2 ** (attempt - 1))` then `actual_sleep = random.uniform(0, computed_delay)`.

For attempt 1: `[0, 1s]`. For attempt 2: `[0, 2s]`. For attempt 3: `[0, 4s]`. Total worst-case: ~7 s before failure.

### Error type

```python
class GeminiTransientError(Exception): ...
class GeminiPermanentError(Exception): ...
```

The embedding adapter catches `genai.types.BlockedError`, `google.api_core.exceptions.ResourceExhausted` (429), `ServiceUnavailable` (503), and `DeadlineExceeded` (504) → `GeminiTransientError`. `google.auth.exceptions.DefaultCredentialsError`, `PermissionDenied` (403) → `GeminiPermanentError`.

### Sleep between calls (separate from retry sleep)

The 0.1 s sleep between successful calls lives in `PreindexUseCase`, NOT in the adapter. The adapter only sleeps on retries. Rationale: keeps the adapter free of preindex-specific pacing policy (it might be called from a runtime use case in `002-mcp-tools` with different pacing needs).

## Consequences

**Positive**:
- One-shot preindex handles transient outages gracefully. Worst-case rebuild cost: +7 s per failed chunk.
- No new dependencies.
- Unit tests can drive retries deterministically by counting calls and asserting delay ranges (test asserts `0 ≤ delay ≤ 1` for first retry).
- Runtime MCP tool calls reuse the same adapter → same retry policy → consistent UX.

**Negative**:
- Preindex with 3 transient errors per chunk over a 1000-chunk rebuild could add ~7000 s (2 h). Mitigation: chunk-hash cache means most retries are skipped; and a single chunk failing 3× is a real signal (likely quota exhaustion), so the build SHOULD fail loudly.
- Full jitter means test timing assertions must allow `[0, max]` ranges.

**Compliance with rules**:
- Spec scenario "Gemini rate-limit error is retried with backoff" — satisfied.
- `rules.apply.guidelines` → "Each class has a single responsibility" — the adapter is HTTP-only; pacing and retry are separate concerns.

## Follow-ups

- Add a `--no-retry` flag to the preindex CLI for debugging (one-shot, fail-fast).
- In `002-mcp-tools`, add a circuit breaker around the runtime Gemini LLM adapter if recruiter demo traffic warrants it.