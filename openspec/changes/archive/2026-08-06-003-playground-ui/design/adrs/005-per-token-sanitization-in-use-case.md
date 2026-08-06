# ADR 005: Per-token sanitization in the use case — not the HTTP middleware

- **Status**: Accepted
- **Date**: 2026-08-06
- **Change**: `003-playground-ui`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

The 5-layer security model mandates that every byte that leaves the server passes through `OutputSanitizer` (Layer 3). Today, the boundary is `OutputSanitizerMiddleware`, which wraps every HTTP response and rewrites the body through the sanitizer before the client sees it.

The chat surface (`/chat/stream`) breaks this middleware. The middleware buffers the full response body before returning it to the client (`sanitizer.py:86-91` reads `response.body_iterator` until exhaustion, then constructs a fresh `Response` from the sanitized bytes). For SSE, this means:

- The client receives the entire stream only after the agent finishes.
- Token-level latency is destroyed: the user sees nothing for ~3.5 s, then the whole answer at once.
- The connection appears dead to the user; the chat UX collapses.

The question is: where does Layer 3 sanitization live for SSE bytes? The middleware is the wrong boundary. The use case is the only correct one.

## Decision Drivers

- **D1**: Layer 3 invariant — every byte that leaves the server passes through `OutputSanitizer`.
- **D2**: Token-level latency preserved. Tokens reach the browser as the agent emits them.
- **D3**: Defense-in-depth for the non-streaming use cases. The middleware is the safety net for routes that don't go through a use-case-level sanitize call.
- **D4**: Audit trail (Layer 5). Every redaction emits one `output.redacted` event with a `pattern=` label.
- **D5**: No double-sanitization. Each byte is sanitized exactly once.

## Considered Options

### Option A — Per-token sanitize inside `AskPortfolioUseCase.astream` (chosen)

The use case yields `AskPortfolioChunk` instances; each token chunk is constructed by calling `self.sanitizer.sanitize(token, source="ask_portfolio")` BEFORE yielding. The middleware skips `/chat/stream` entirely (per the `sanitizer-skip-list` spec, 7-tuple at `sanitizer.py:39`).

```python
# src/mcp_server/application/use_cases/ask_portfolio.py
async def astream(self, request):
    if not self.rate_limiter.check(request.client_ip):
        raise RateLimitExceeded(...)
    accumulated: list[str] = []
    tools_called: list[str] = []
    async for chunk in self.agent.stream(AgentRequest(...), self.tools):
        if chunk.kind == "token":
            sanitized = self.sanitizer.sanitize(chunk.data, source="ask_portfolio")
            accumulated.append(sanitized.redacted_text)
            yield AskPortfolioChunk(kind="token", answer_token=sanitized.redacted_text)
        elif chunk.kind == "tool_call":
            tools_called.append(chunk.data["name"])
            self.audit.warn("agent.tool_call", tool=chunk.data["name"], source="ask_portfolio")
    yield AskPortfolioChunk(
        kind="done",
        result=AskPortfolioResult(
            answer="".join(accumulated),
            tools_called=tools_called,
            conversation_id=request.conversation_id,
        ),
    )
```

**Pros**:
- Layer 3 invariant holds per chunk. Every byte that leaves the server is sanitized exactly once.
- Token-level latency preserved. Tokens reach the browser as the agent emits them (sub-second in mock mode, ~700 ms per token in real mode).
- Audit emission is per-token (one `output.redacted` per redacted token; one `agent.tool_call` per tool invocation).
- The middleware is the defense-in-depth safety net for non-streaming routes. Skipping `/chat/stream` is safe because the use case owns the sanitize call.

**Cons**:
- The sanitization responsibility moves from the boundary to the use case. A future use case author who forgets the per-chunk `sanitize` call breaks the invariant for SSE. Mitigated by:
  - A unit test (`tests/unit/application/use_cases/test_astream_ask_portfolio.py`) that injects a fake `OutputSanitizer` and asserts the call.
  - The middleware skip-list test that pins the 7-tuple.
- The 5 non-streaming use cases (`list_projects`, `search_code`, `explain_architecture`, `summarize_readme`, `get_architecture_diagram`) already call `sanitize(...)` per their existing `mcp-tools` specs (ADR-003 in `002-mcp-tools`). The middleware skips `/playground/api/*` to avoid double-sanitization; the use case is the only sanitize pass for playground fragments.

### Option B — Streaming-aware middleware (rejected)

Rewrite the middleware to consume `response.body_iterator` chunk-by-chunk, sanitize each chunk, and yield it.

**Pros**:
- One sanitize location (the boundary).
- Use cases stay pure.

**Cons**:
- **JSON-detection is per-chunk fragile.** The middleware currently sniffs the first byte (`{` or `[`) to decide between `sanitize_json` and `sanitize`. SSE tokens are JSON-shaped but the heuristic breaks across chunk boundaries.
- **Buffering is still required** for `sanitize_json` (regex over a JSON value requires the value to be complete). The middleware would have to maintain a partial-JSON buffer, which is the same buffering problem the use-case approach avoids.
- **Defense-in-depth is lost.** If the boundary sanitizer has a bug, every byte leaks. The use-case approach gives us two independent sanitization points (the use case, then the middleware for non-skipped routes).
- **Audit emission becomes ambiguous.** Per-chunk audit events lose the `source=` label that the use case provides.

### Option C — Sanitize at the SSE encoder (rejected)

Add a sanitization pass in the chat route's `event_generator` before yielding SSE bytes.

**Pros**:
- Closer to the boundary (still inside the HTTP adapter).
- Use case stays pure.

**Cons**:
- **Hexagonal violation.** The chat route (`interfaces/http/web/chat.py`) is an HTTP adapter. Sanitization is an application-layer concern (Layer 3 of the 5-layer model). Moving it to the adapter breaks the layered discipline.
- **Two boundaries to keep in sync.** The MCP tool wrappers also need sanitization; if sanitization lives at the HTTP adapter for chat and at the use case for MCP, drift is inevitable.
- **Audit emission** still needs a `source=` label that the route doesn't know about.

### Option D — Sanitize the entire stream after the agent finishes (rejected)

Buffer the agent's output in the use case, sanitize the concatenated string at the end, then yield the whole thing in one chunk.

**Pros**:
- Simple implementation.

**Cons**:
- **Defeats the entire point of streaming.** The user sees nothing for ~3.5 s, then the whole answer at once.
- **Doesn't satisfy the `agent-streaming` spec** ("Per-token sanitization in `AskPortfolioUseCase.astream` — Layer 3 invariant").

## Decision

**Option A.** Per-token sanitize inside `AskPortfolioUseCase.astream`. The middleware skips `/chat/stream` (and `/chat`, `/playground`, `/playground/api`) per the `sanitizer-skip-list` spec; the use case is the only sanitize pass for SSE bytes. The middleware remains the defense-in-depth safety net for routes that don't go through a use-case-level sanitize call (e.g., a future `/admin/debug` route).

The Layer 3 invariant is preserved by contract:
- Every byte on `/chat/stream` was sanitized in `AskPortfolioUseCase.astream` before being yielded.
- Every byte on `/playground/api/*` was sanitized inside the corresponding use case (already true per `002-mcp-tools` ADR-003).
- Every byte on a future non-skipped route is sanitized by the middleware.

The audit trail is unchanged: `output.redacted` events emit per sanitize call (one per token, not one per stream). `agent.tool_call` events emit per tool invocation (per the `agent-streaming` spec).

## Consequences

**Positive**:
- Layer 3 invariant holds per chunk for SSE.
- Token-level latency preserved (sub-second in mock, ~700 ms per token in real).
- Audit emission is per-token (one event per match, not one per stream).
- The middleware's skip-list change is local to `sanitizer.py:39` (4 new entries, 1 test update).
- The use case remains the single source of truth for what "Layer 3" means in this context.

**Negative**:
- Sanitization responsibility moves from the boundary to the use case. A future use case author who writes an SSE-style method without a per-chunk `sanitize` call breaks the invariant. Mitigated by unit tests + the middleware safety net for non-skipped routes.
- The middleware's skip-list grows from 2 entries to 7. Any future closed-world invariant on the skip-list must be updated (the test pins the 7-tuple literal).

## Compliance with rules

- `rules.apply.guidelines` → "Hexagonal architecture is mandatory" — satisfied; sanitization is an application-layer concern (the use case), the HTTP middleware is the defense-in-depth boundary.
- `invariants` → "5-layer security model is mandatory" — satisfied; Layer 3 fires per-token in the use case; Layer 5 audit emission fires per match.
- `rules.specs` → "Any new tool MUST include a security redaction test scenario" — the `agent-streaming` spec has three redaction scenarios (AWS key, clean pass, non-deferred per-token); the `sanitizer-skip-list` spec has eight scenarios.

## Follow-ups

- In apply phase: write `tests/unit/application/use_cases/test_astream_ask_portfolio.py` with a fake `OutputSanitizer` that records every call; assert the use case calls it once per token (not once for the concatenated total).
- In apply phase: extend `tests/integration/test_sanitizer_middleware.py` to assert the 7-tuple and that the middleware skips `/chat/stream`.
- In verify phase: log the agent's audit events for a real recruiter demo and confirm one `output.redacted` per redacted token, one `agent.tool_call` per tool invocation, and zero double-events.
