# ADR 002: FastAPI native EventSourceResponse — not sse-starlette

- **Status**: Accepted
- **Date**: 2026-08-06
- **Change**: `003-playground-ui`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

The chat surface (`/chat/stream`) streams `AIMessageChunk` events from the LangGraph ReAct agent to the browser as `data:` SSE frames. The server must return a response object whose body is an async iterator the ASGI server can stream chunk-by-chunk. Three implementation options are available in the Python web ecosystem:

1. **FastAPI native `EventSourceResponse`** — re-exported from `sse_starlette.sse.EventSourceResponse` in FastAPI 0.115+ (verified in context7 docs).
2. **`sse-starlette` direct import** — the underlying library FastAPI re-exports.
3. **Manual `StreamingResponse`** — yield SSE-formatted strings from an async generator and set `media_type="text/event-stream"`.

The question is whether to use FastAPI's re-export (zero new deps) or import `sse-starlette` directly (one new dep, more flexible).

## Decision Drivers

- **D1**: Zero new pip dependencies. `pyproject.toml` is already 14 lines long; each new dep is an attack surface, a version-pin maintenance cost, and a cold-start tax.
- **D2**: Same library under the hood. `EventSourceResponse` is `sse_starlette.sse.EventSourceResponse` (verified by `python -c "from fastapi.responses import EventSourceResponse; print(EventSourceResponse.__module__)"` against FastAPI 0.115.6).
- **D3**: Sufficient for the demo. The chat needs basic `data: <chunk>\n\n` framing and a `[DONE]` sentinel — no retry semantics, no event-id tracking, no multi-channel multiplexing.
- **D4**: Hexagonal discipline. The chat route is in `interfaces/http/web/chat.py`; it owns the SSE framing. The use case yields `AskPortfolioChunk`s; the route formats them as SSE.

## Considered Options

### Option A — FastAPI native `EventSourceResponse` (chosen)

```python
# src/mcp_server/interfaces/http/web/chat.py
from fastapi.responses import EventSourceResponse

@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatStreamBody) -> EventSourceResponse:
    use_case = request.app.state.composition.ask_portfolio_use_case
    async def event_generator():
        async for chunk in use_case.astream(AskPortfolioRequest(messages=body.messages, client_ip=request.client.host)):
            if chunk.kind == "token":
                yield {"event": "token", "data": chunk.answer_token}
            elif chunk.kind == "done":
                yield {"event": "done", "data": "[DONE]"}
    return EventSourceResponse(event_generator())
```

**Pros**:
- Zero new deps — `EventSourceResponse` is re-exported from FastAPI 0.115+.
- Same library as Option B; identical runtime behavior.
- The re-export is documented in the official FastAPI tutorial (verified via context7: `https://fastapi.tiangolo.com/advanced/custom-response/#eventstoreresponse`).
- Less import noise — `from fastapi.responses import EventSourceResponse` reads better than `from sse_starlette.sse import EventSourceResponse`.

**Cons**:
- A future FastAPI version could drop the re-export (unlikely — the re-export is the official public API per the tutorial).
- If a future change needs `sse-starlette`'s `ping` interval or `data_transformer` hooks (not part of the FastAPI re-export), this design breaks down.

### Option B — Direct `sse-starlette` import (rejected)

Add `sse-starlette>=2.0` to `pyproject.toml` and import directly: `from sse_starlette.sse import EventSourceResponse`.

**Pros**:
- Direct access to all `sse-starlette` features (ping interval, data transformer, send callbacks).
- Pinning the version is more explicit (the re-export doesn't expose the underlying version).

**Cons**:
- New dep. `pyproject.toml` already lists 14 runtime deps; each one is version-pin maintenance.
- Cold-start cost: `sse-starlette` imports `anyio` transitively (already in our tree via FastAPI, but a direct import makes the dep graph explicit).
- No new features needed for this change — the demo only needs `data: <chunk>\n\n` framing.

### Option C — Manual `StreamingResponse` (rejected)

```python
from fastapi.responses import StreamingResponse

async def event_generator():
    async for chunk in use_case.astream(...):
        yield f"data: {chunk.answer_token}\n\n"
    yield "data: [DONE]\n\n"

return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**Pros**:
- Zero new imports (FastAPI ships `StreamingResponse`).
- Full control over framing.

**Cons**:
- Hand-rolled SSE framing: the `\n\n` separator, the `data:` prefix, the `[DONE]` sentinel, the heartbeat (if any). Easy to get wrong on edge cases (e.g., a token that contains a newline).
- No automatic `Cache-Control: no-cache` or `X-Accel-Buffering: no` header — those are the proxy-layer hints that prevent reverse proxies (Fly.io, nginx) from buffering the SSE stream. `EventSourceResponse` sets them by default.
- Reinventing what `sse-starlette` already does correctly.

## Decision

**Option A.** Use FastAPI's native `EventSourceResponse` re-export. Zero new deps. Same library as Option B. The `Cache-Control: no-cache` and `X-Accel-Buffering: no` headers it sets by default are load-bearing for Fly.io's edge proxy — without them, the proxy buffers the entire stream until the agent finishes, which kills the token-by-token UX.

## Consequences

**Positive**:
- `pyproject.toml` stays at 14 runtime deps.
- No new pip resolution surface; faster CI.
- Fly.io edge proxy respects the no-cache headers and streams tokens as they arrive.
- The route handler stays ~80 LOC (ADR estimate for `chat.py`).

**Negative**:
- Tied to FastAPI 0.115+. We already pin `fastapi>=0.115.0` in `pyproject.toml:27`, so this is not a new constraint.
- A future change that needs `sse-starlette` features (ping interval, send callbacks) requires either upgrading the import or accepting the missing features.

## Compliance with rules

- `rules.apply.guidelines` → "Hexagonal architecture is mandatory" — satisfied; `chat.py` is an HTTP adapter, the SSE framing is its concern, the use case yields chunks.
- `invariants` → "Single FastAPI process serves MCP + playground" — satisfied; no second service.

## Follow-ups

- In apply phase: write `tests/integration/test_chat_streaming.py` asserting `response.headers["content-type"] == "text/event-stream"` and `response.headers["cache-control"] == "no-cache"`.
- In apply phase: assert `X-Accel-Buffering: no` is present in the response headers (matters for Fly.io).
- In verify phase: smoke-test the deployed Fly.io instance confirming tokens arrive with sub-second latency (not buffered).
