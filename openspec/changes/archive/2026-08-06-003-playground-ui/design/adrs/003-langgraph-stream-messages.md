# ADR 003: LangGraph stream_mode="messages" with major-version pin

- **Status**: Accepted
- **Date**: 2026-08-06
- **Change**: `003-playground-ui`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

`/chat/stream` consumes the LangGraph ReAct agent's output as a token stream. LangGraph exposes four `stream_mode` values (`"values"`, `"updates"`, `"debug"`, `"messages"`, plus `"events"`), each with a different event shape. The chat must:

1. Yield one SSE `data:` event per model token the agent emits.
2. Skip tool-only events (so the user sees only the assistant's prose, not the tool dispatch noise).
3. Terminate with a `[DONE]` sentinel when the agent finishes.
4. Tolerate the mock adapter (5 fake tokens, no LLM) identically to the real adapter.

The `stream_mode` choice determines the event surface and the code we write to filter it. A version pin determines whether the contract can drift mid-release.

## Decision Drivers

- **D1**: Token-per-event granularity. The chat UX relies on seeing text appear one or two words at a time, not full message dumps.
- **D2**: Clean event shape. Tool calls and LLM reasoning steps must be filterable so the SSE stream carries only the assistant prose.
- **D3**: Stable contract. LangGraph is a fast-moving library; the `stream_mode` event shapes have changed across 0.x releases. The demo must not break when `pip install -U` upgrades LangGraph.
- **D4**: Mock parity. The `_MockLangChainAgentAdapter` must produce the same `AgentChunk` shape as the real adapter so the SSE encoder is identical for both paths.

## Considered Options

### Option A — `stream_mode="messages"` with `langgraph>=0.2,<2.0` (chosen)

`stream_mode="messages"` yields `(message, metadata)` tuples as each LLM message (Human / AI / Tool) is produced. The chat path filters for `AIMessageChunk` and yields one `AgentChunk(kind="token", data=str(content))` per chunk.

```python
# src/mcp_server/infrastructure/langchain.py
async def stream(self, request, tools):
    agent = create_react_agent(self._llm, [t.fn if hasattr(t, "fn") else t for t in tools])
    messages = [*(request.history or []), {"role": "user", "content": request.question}]
    async for message, _meta in agent.astream(
        {"messages": messages},
        config={"recursion_limit": request.max_tool_calls * 2 + 1},
        stream_mode="messages",
    ):
        if isinstance(message, AIMessageChunk):
            yield AgentChunk(kind="token", data=str(message.content))
    yield AgentChunk(kind="done", data="")
```

Pin `langgraph>=0.2,<2.0` in `pyproject.toml:35` (currently `>=0.2.0`; tighten the upper bound).

**Pros**:
- `stream_mode="messages"` is the documented "stable" surface in the LangGraph streaming guide (verified via context7: `https://langchain-ai.github.io/langgraph/concepts/streaming/`).
- Yields `AIMessageChunk` events that match the LangChain core message types — same shape across the LangChain + LangGraph ecosystem.
- Filtering for `AIMessageChunk` cleanly excludes ToolMessage, HumanMessage, and tool dispatch noise.
- The major-version pin (`<2.0`) freezes the contract. LangGraph 1.x → 2.x would be a breaking change to the streaming event shape; the pin prevents surprise upgrades.

**Cons**:
- The pin `<2.0` requires a deliberate upgrade PR when LangGraph 2.x lands. That's a feature, not a bug (we want the upgrade to be a reviewed PR with migration tests).
- Filtering for `AIMessageChunk` only — a future LangChain release that splits `AIMessageChunk` into multiple subclasses (e.g., `AIMessageTextChunk` vs `AIMessageToolChunk`) would require a filter update. Pinning the version makes this a deliberate event, not a silent breakage.

### Option B — `stream_mode="values"` (rejected)

Yields the full state dict after each node. Each event contains the entire message list, not just the new token. The chat would have to diff message lists to extract the delta — wasted work, and the diff logic is fragile.

**Pros**:
- Easier mental model ("after each step, here's the full state").

**Cons**:
- No token granularity — the chat would receive full message dumps, not incremental tokens.
- Diffing messages to extract the delta is brittle (chat bubbles flicker, ordering matters).
- Wastes bandwidth — every event carries the full history.

### Option C — `stream_mode="events"` (rejected)

Low-level node-level events (`on_chain_start`, `on_llm_stream`, `on_tool_end`, etc.). Maximum flexibility.

**Pros**:
- Full control — every LangGraph internal event is available.

**Cons**:
- Verbose. Tool dispatch, retriever calls, and LLM chunks all arrive as separate events. The chat would need a substantial filter to extract just the assistant text.
- More fragile to version changes — `events` is the surface most likely to change across releases.
- Overkill for a chat that only needs to render the assistant's prose.

### Option D — No version pin (rejected)

Keep `langgraph>=0.2.0` (no upper bound).

**Pros**:
- No upgrade PRs needed.

**Cons**:
- `pip install -U` on a recruiter's machine or in CI could pull LangGraph 2.x and silently change the event shape. The chat would stop working with no local change.
- The whole point of pinning is to make breaking changes deliberate.

## Decision

**Option A.** Use `stream_mode="messages"`; pin `langgraph>=0.2,<2.0`; filter for `AIMessageChunk`. The mock adapter produces the same `AgentChunk` shape with five hard-coded tokens.

The version pin enforcement is a regression test in `tests/unit/test_pyproject.py` that asserts the installed `langgraph` version satisfies the range, and an integration test that asserts the chunk shape (`AIMessageChunk` with a `.content` attribute).

## Consequences

**Positive**:
- Token granularity matches the chat UX requirement.
- Clean filter — one `isinstance(message, AIMessageChunk)` check.
- Mock parity is trivial (the mock doesn't go through LangGraph at all).
- Major-version upgrade is a deliberate PR with regression coverage.

**Negative**:
- A deliberate upgrade PR is required when LangGraph 2.x stabilizes.
- The mock adapter's 5 fake tokens are a separate code path; if the real adapter's event shape ever changes, the mock must be updated to mirror it. Mitigated by the `AgentPort` protocol + shared `AgentChunk` Pydantic model.

## Compliance with rules

- `rules.apply.guidelines` → "Hexagonal architecture is mandatory" — satisfied; `AgentPort.stream` is a port; the LangChain adapter is one implementation; the mock is another.
- `rules.proposal` → "Include rollback plan for risky changes" — version pin is the rollback plan (revert the upper bound to `>=0.2.0` if 2.x never stabilizes).

## Follow-ups

- In apply phase: write `tests/unit/infrastructure/test_langchain_adapter_stream.py` with a mocked LangGraph `agent.astream(...)` returning fake `(AIMessageChunk, meta)` tuples; assert the adapter yields one `AgentChunk(kind="token")` per chunk.
- In apply phase: write `tests/unit/test_pyproject.py` asserting `langgraph` is in `[0.2, 2.0)`.
- In verify phase: log the LangGraph version in the deployed Fly.io instance startup log so version drift is visible.
