# agent-streaming — Delta Specification

## Purpose

The streaming variant of `AskPortfolioUseCase` plus the
`AgentPort.stream` protocol method that powers it. The MCP `ask_portfolio`
tool at `/mcp` continues to use the existing buffered `execute` /
`aexecute` path (final answer only); `astream` is purely additive for the
browser playground's `/chat/stream` route.

Per Decision #4 the streaming implementation uses
`stream_mode="messages"` on the LangGraph ReAct agent and yields
`AIMessageChunk` events. Per Decision #6 the use case must apply
Layer 3 sanitization per-token before yielding — the middleware can no
longer catch SSE bytes (the middleware buffers full bodies; see the
`sanitizer-skip-list` spec). Per Decision #5 the mock agent (active when
`GEMINI_API_KEY` is empty) streams 5 deterministic fake tokens with
`asyncio.sleep(0.05)` simulated latency so the demo works without an API
key.

`langgraph>=0.2,<2.0` is pinned in `pyproject.toml:35` (proposal §
Dependencies) to keep the streaming event surface stable.

## Schema / Interface

```python
# src/mcp_server/application/ports/agent.py — additions
from collections.abc import AsyncIterator
from typing import Literal

class AgentChunk(BaseModel):
    # "error" added by REL-3 (PR2a mid-stream exception handling): the
    # SSE layer translates an AgentChunk(kind="error", data=<str>) to a
    # terminal `data: [ERROR]\n\n` event so the client can render an
    # inline retry affordance without parsing arbitrary payloads.
    kind: Literal["token", "tool_call", "done", "error"]
    data: str | dict[str, Any] = ""

class AgentPort(Protocol):
    async def run(self, request: AgentRequest, tools: list[Any]) -> AgentResponse: ...
    async def stream(self, request: AgentRequest, tools: list[Any]) -> AsyncIterator[AgentChunk]: ...

# src/mcp_server/application/use_cases/ask_portfolio.py — additions
@dataclass(frozen=True)
class AskPortfolioChunk:
    # "error" added by REL-3: AskPortfolioChunk(kind="error", error=str(exc))
    # is yielded exactly once when the agent raises mid-stream so the SSE
    # layer can translate it to a final `data: [ERROR]\n\n` frame.
    kind: Literal["token", "tool_call", "done", "error"]
    answer_token: str | None = None        # set when kind == "token"
    tool_call: dict[str, Any] | None = None # set when kind == "tool_call"
    result: AskPortfolioResult | None = None  # set when kind == "done"
    error: str | None = None               # set when kind == "error" (REL-3)
```

```python
# src/mcp_server/infrastructure/langchain.py — additions
from langchain_core.messages import AIMessageChunk

class _MockLangChainAgentAdapter:
    async def stream(self, request, tools) -> AsyncIterator[AgentChunk]:
        for token in ("Tok", "en", "ized", " mock", " answer"):
            await asyncio.sleep(0.05)
            yield AgentChunk(kind="token", data=token)
        yield AgentChunk(kind="done", data="")

class LangChainAgentAdapter:
    async def stream(self, request, tools) -> AsyncIterator[AgentChunk]:
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

## ADDED Requirements

### Requirement: AgentPort.stream Returns AgentChunk Stream

`AgentPort` MUST declare `async def stream(request: AgentRequest, tools:
list[Any]) -> AsyncIterator[AgentChunk]`. `AgentChunk` MUST be a Pydantic
model with `kind: Literal["token", "tool_call", "done", "error"]` and
`data: str | dict`. The `"error"` kind (REL-3, added by PR2a's mid-stream
exception handling) carries the stringified exception in `data` and is
translated by the SSE layer to a terminal `data: [ERROR]\n\n` event so
the client can render an inline retry affordance. The method MUST be
implemented by both `LangChainAgentAdapter` and
`_MockLangChainAgentAdapter`.

#### Scenario: Both adapters implement stream

- GIVEN the composition root has selected either the real or mock adapter
- WHEN `adapter.stream(AgentRequest(...), tools)` is awaited
- THEN it MUST return an async iterator of `AgentChunk` instances
- AND every yielded object MUST have a valid `kind` value
  (`"token"`, `"tool_call"`, `"done"`, or `"error"` per the closed set
  extended by REL-3).

#### Scenario: Stream is additive (run still works)

- GIVEN the composition root has wired the agent
- WHEN `adapter.run(...)` is invoked
- THEN it MUST return an `AgentResponse` exactly as before
- AND MUST NOT depend on `stream` being implemented.

### Requirement: LangChainAgentAdapter.stream Uses stream_mode="messages"

`LangChainAgentAdapter.stream` MUST invoke
`agent.astream(input, config, stream_mode="messages")` and MUST yield an
`AgentChunk(kind="token", data=...)` for each `AIMessageChunk` event. It
MUST ignore non-AI message types (HumanMessage, ToolMessage, etc.) so the
client only sees model tokens.

#### Scenario: AI tokens are yielded

- GIVEN a Gemini-backed `LangChainAgentAdapter`
- WHEN `stream(...)` is called with a user question
- THEN it MUST yield exactly one `AgentChunk(kind="token", data=<chunk>)`
  per `AIMessageChunk` from the agent
- AND MUST skip non-AI messages (no `AgentChunk` for tool-only messages).

#### Scenario: stream_mode="messages" is the only mode used

- GIVEN any invocation of `LangChainAgentAdapter.stream`
- WHEN the underlying LangGraph call is inspected
- THEN the `stream_mode` kwarg MUST equal `"messages"`
- AND the recursion limit MUST equal `request.max_tool_calls * 2 + 1`.

#### Scenario: langgraph version pin prevents event-surface drift

- GIVEN `pyproject.toml` lists `langgraph>=0.2,<2.0`
- WHEN `pip install -e ".[dev]"` runs
- THEN the installed `langgraph` version MUST satisfy that range
- AND a regression test MUST pin the chunk shape (`AIMessageChunk` with
  `.content`).

### Requirement: Mock Agent Streams 5 Tokens + DONE

`_MockLangChainAgentAdapter.stream` MUST yield exactly 5 token chunks
(`"Tok"`, `"en"`, `"ized"`, `" mock"`, `" answer"`), each separated by
`asyncio.sleep(0.05)` to simulate network latency, followed by one
`AgentChunk(kind="done", data="")`.

#### Scenario: Mock stream yields 5 tokens + DONE

- GIVEN the mock agent is active (no `GEMINI_API_KEY`)
- WHEN `stream(...)` is awaited
- THEN it MUST yield exactly 5 `AgentChunk(kind="token", ...)` events
- AND the tokens MUST equal `("Tok", "en", "ized", " mock", " answer")`
  in that order
- AND a final `AgentChunk(kind="done", data="")` MUST be yielded.

#### Scenario: Mock tokens are spaced by asyncio.sleep(0.05)

- GIVEN the mock agent is active
- WHEN the stream is collected with monotonic timestamps
- THEN the wall-clock between consecutive tokens MUST be ≥ 0.05 s
- AND total elapsed time for the 5 tokens MUST be ≥ 0.25 s.

### Requirement: AskPortfolioUseCase.astream Enforces the Same Rate-Limit Gate

`AskPortfolioUseCase.astream` MUST call
`self.rate_limiter.check(request.client_ip)` exactly once per request
before iterating the agent's stream (same gate as `aexecute`). When the
limiter returns `False`, the method MUST raise `RateLimitExceeded`
without invoking the agent.

#### Scenario: Rate-limit gate fires once per request

- GIVEN a fresh client IP
- WHEN `astream(request)` is called for the first time
- THEN `self.rate_limiter.check(request.client_ip)` MUST be called
  exactly once
- AND on the 31st call within 60 s from the same IP, the method MUST
  raise `RateLimitExceeded`
- AND no agent call MUST occur after the gate fails.

### Requirement: AskPortfolioUseCase.astream Sanitizes Per Token (Layer 3)

`AskPortfolioUseCase.astream` MUST call
`self.sanitizer.sanitize(token, source="ask_portfolio")` on EACH token
chunk BEFORE yielding the chunk to the caller. The Layer 3 invariant
holds per-chunk — the middleware can no longer catch SSE bytes (see the
`sanitizer-skip-list` spec).

#### Scenario: Token containing AWS-shaped key is redacted

- GIVEN the agent emits a token `AKIAIOSFODNN7EXAMPLE` mid-stream
- WHEN `astream` yields the token chunk
- THEN the chunk's `answer_token` MUST contain `[REDACTED]` in place of
  the key
- AND a `RedactionIncident` with `pattern=aws` MUST be emitted to the
  audit log.

#### Scenario: Clean tokens pass through unchanged

- GIVEN the agent emits a token `Recruiter-friendly answer text`
- WHEN `astream` yields the token chunk
- THEN the chunk's `answer_token` MUST equal the input verbatim
- AND no `RedactionIncident` MUST be emitted for that token.

#### Scenario: Per-token sanitization is not deferred

- GIVEN a stream of 5 clean tokens
- WHEN `astream` yields each chunk
- THEN `sanitize` MUST be called once per token (not once for the
  concatenated total)
- AND the audit log MUST show one `output.redacted` event per redaction,
  not one per stream.

### Requirement: AskPortfolioUseCase.astream Emits Tool-Call Audit Events

When the agent invokes a sibling tool, `astream` MUST emit the same
`audit.info("agent.tool_call", tool=<name>, source="ask_portfolio")`
event that `aexecute` emits. The event MUST be fired exactly once per
tool invocation, mirroring `aexecute`'s contract.

#### Scenario: Tool calls emit one audit event each

- GIVEN the agent calls `list_projects` and then `search_code`
- WHEN `astream` finishes iterating
- THEN the audit log MUST contain exactly two `agent.tool_call` events
  (one per tool), with `source="ask_portfolio"`.

#### Scenario: No audit event for tokens

- GIVEN the agent emits 5 token chunks
- WHEN `astream` finishes iterating
- THEN the audit log MUST NOT contain `agent.tool_call` events for any
  token (tokens are not tool calls).

### Requirement: AskPortfolioUseCase.astream Terminates With Final Result

`astream` MUST yield a final `AskPortfolioChunk(kind="done", result=...)`
when the agent finishes. The `result` MUST be an `AskPortfolioResult`
whose `answer` is the sanitized concatenation of all token chunks, with
`tools_called` populated and `conversation_id` echoed from the request.

#### Scenario: DONE chunk carries sanitized final result

- GIVEN the agent emits 5 tokens spelling "Tokenized mock answer" and
  invokes one tool
- WHEN `astream` finishes
- THEN the final chunk MUST be `AskPortfolioChunk(kind="done", result=
  AskPortfolioResult(answer="Tokenized mock answer", tools_called=
  ["list_projects"], conversation_id=None))`
- AND `result.answer` MUST equal the concatenation of the sanitized
  tokens.

#### Scenario: Tool call appears in result.tools_called

- GIVEN the agent invokes `search_code`
- WHEN the DONE chunk is yielded
- THEN `result.tools_called` MUST contain `"search_code"`.

### Requirement: AskPortfolioUseCase.astream Yields an ERROR Chunk on Mid-Stream Exception (REL-3)

When the underlying `agent.stream(...)` raises an exception (LangGraph
recursion-limit abort, Gemini rate-limit, network timeout, etc.), `astream`
MUST yield exactly one `AskPortfolioChunk(kind="error", error=str(exc))` as
the terminal event and MUST NOT yield a partial `AskPortfolioResult`. The
SSE layer (`interfaces/http/web/chat.py`) translates this to a final
`data: [ERROR]\n\n` frame so the client can render an inline "connection
lost, retry?" affordance. The client MUST NOT append the partial
assistant text to `localStorage` (per `chat-persistence` spec).

#### Scenario: Mid-stream exception yields a single ERROR chunk

- GIVEN the agent raises `RecursionLimitExceeded` after 2 token chunks
- WHEN `astream` is iterated
- THEN it MUST yield exactly 2 `kind="token"` chunks (one per delivered
  `AIMessageChunk`)
- AND MUST yield exactly 1 terminal `AskPortfolioChunk(kind="error",
  error="RecursionLimitExceeded(...)")`
- AND MUST NOT yield any `kind="done"` chunk.

#### Scenario: Mid-stream exception does not yield a partial result

- GIVEN the agent raises mid-stream
- WHEN the caller inspects the iterator
- THEN no yielded chunk MUST have a non-`None` `result` field
- AND `AskPortfolioResult` MUST NOT be constructed for the failed stream.

## Error / Edge Cases

- `stream_mode="messages"` event with no `.content` attribute (empty
  chunk): MUST yield an `AgentChunk(kind="token", data="")` rather than
  raising (so the client doesn't see a disconnect on benign chunks).
- Agent raises an exception mid-stream: `astream` MUST yield exactly one
  `AskPortfolioChunk(kind="error", error=str(exc))` as the terminal
  event (REL-3 amendment to the closed set). The SSE layer translates
  this to a final `data: [ERROR]\n\n` frame per the `playground-ui`
  spec; no partial `AskPortfolioResult` MUST be yielded. The client
  does NOT append the partial assistant text to `localStorage`.
- `--workers 1` is mandatory (per `app-bootstrap`) so the in-process
  rate-limit state is consistent across `/chat` and `/mcp` calls.
- `_MockLangChainAgentAdapter.stream` MUST NOT make any outbound HTTP
  call (zero network in mock mode).

## Test Scenarios

| Scenario | Required because |
|---|---|
| `_MockLangChainAgentAdapter.stream` yields 5 tokens + DONE | Mock streaming contract (Decision #5) |
| `LangChainAgentAdapter.stream` uses `stream_mode="messages"` and yields `AIMessageChunk`-only | LangGraph integration |
| `AskPortfolioUseCase.astream` sanitizes each token before yielding | **Layer 3** per-chunk invariant (Decision #6) |
| Rate-limit gate fires once and blocks 31st request | **Layer 5** rate limit |
| Tool-call audit events emitted exactly once per invocation | **Layer 5** audit trail |
| DONE chunk carries sanitized `AskPortfolioResult` with `tools_called` | Final-result contract |
| `langgraph` version pin enforced by `tests/unit/test_pyproject.py` | Pin drift guard |
| MCP `/mcp` `ask_portfolio` continues to use `aexecute` (regression) | Buffered path unchanged |
| Mid-stream exception yields exactly 1 `kind="error"` chunk and no partial result | REL-3 contract |
| SSE layer translates `kind="error"` to terminal `data: [ERROR]\n\n` | REL-3 wiring |
