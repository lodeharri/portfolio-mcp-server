from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from mcp_server.application.ports.agent import (
    AgentChunk,
    AgentRequest,
    AgentResponse,
)
from mcp_server.application.use_cases.ask_portfolio import (
    DEFAULT_MAX_TOOL_CALLS,
    AskPortfolioChunk,
    AskPortfolioRequest,
    AskPortfolioUseCase,
)
from mcp_server.domain.exceptions import RateLimitExceeded
from mcp_server.security.output_sanitizer import OutputSanitizer


class FakeAgent:
    def __init__(self, response: AgentResponse | None = None) -> None:
        self.response = response or AgentResponse(answer="clean answer")
        self.calls: list[tuple[AgentRequest, list[Any]]] = []

    async def run(self, request: AgentRequest, tools: list[Any]) -> AgentResponse:
        self.calls.append((request, tools))
        return self.response


class FakeRateLimiter:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[str] = []

    def check(self, client_ip: str) -> bool:
        self.calls.append(client_ip)
        return self.allow


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warn(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def make_use_case(
    agent: FakeAgent | None = None,
    rate_limiter: FakeRateLimiter | None = None,
) -> tuple[AskPortfolioUseCase, FakeAgent, FakeAudit, FakeRateLimiter]:
    agent = agent or FakeAgent()
    audit = FakeAudit()
    limiter = rate_limiter or FakeRateLimiter()
    use_case = AskPortfolioUseCase(
        agent=agent,
        tools=["tool"],
        sanitizer=OutputSanitizer(audit=audit),
        audit=audit,
        rate_limiter=limiter,
    )
    return use_case, agent, audit, limiter


def test_runs_agent_port_and_returns_sanitized_answer() -> None:
    use_case, agent, _, limiter = make_use_case()

    result = use_case.execute(AskPortfolioRequest(question="Which project?"))

    assert result.answer == "clean answer"
    assert limiter.calls == ["127.0.0.1"]
    assert agent.calls[0][0].max_tool_calls == DEFAULT_MAX_TOOL_CALLS
    assert agent.calls[0][1] == ["tool"]


def test_default_max_tool_calls_is_three() -> None:
    """``DEFAULT_MAX_TOOL_CALLS`` MUST be 3 — the budget that fixes the
    "Recursion limit of 11" loop.

    A 5-call budget lets the agent burn through tools without ever
    synthesizing a final answer; 3 is the largest value that, paired
    with the portfolio system prompt's "Stop after 2-3 tool calls" rule,
    gives the agent room to finish with a non-tool answer.

    The downstream ``recursion_limit = max_tool_calls * 3 + 1`` formula
    (``LangChainAgentAdapter``) gives the graph 10 steps at this value
    — one extra "answer attempt" step beyond the 6 of strict parity.
    """
    assert DEFAULT_MAX_TOOL_CALLS == 3, (
        f"DEFAULT_MAX_TOOL_CALLS must be 3 to keep the ReAct loop "
        f"bounded; got {DEFAULT_MAX_TOOL_CALLS}"
    )


def test_default_max_tool_calls_is_threaded_into_agent_request() -> None:
    """The use case MUST thread ``DEFAULT_MAX_TOOL_CALLS`` into the
    ``AgentRequest`` it sends to the agent port (regression guard for
    the value, not just the constant).
    """
    use_case, agent, _, _ = make_use_case()

    use_case.execute(AskPortfolioRequest(question="Search"))

    assert len(agent.calls) == 1
    sent_request = agent.calls[0][0]
    assert sent_request.max_tool_calls == DEFAULT_MAX_TOOL_CALLS
    assert sent_request.max_tool_calls == 3


def test_audits_tool_calls() -> None:
    agent = FakeAgent(AgentResponse(answer="answer", tool_calls=[{"name": "search_code"}]))
    use_case, _, audit, _ = make_use_case(agent=agent)

    result = use_case.execute(AskPortfolioRequest(question="Search"))

    assert result.tools_called == ["search_code"]
    assert ("agent.tool_call", {"tool": "search_code", "source": "ask_portfolio"}) in audit.events


def test_redacts_agent_answer() -> None:
    agent = FakeAgent(AgentResponse(answer="Key: AKIAIOSFODNN7EXAMPLE"))
    use_case, _, _, _ = make_use_case(agent=agent)

    result = use_case.execute(AskPortfolioRequest(question="Show key"))

    assert result.answer == "Key: [REDACTED]"


@pytest.mark.parametrize("question", ["", "   "])
def test_rejects_empty_question(question: str) -> None:
    use_case, _, _, _ = make_use_case()

    with pytest.raises(ValueError, match="non-empty"):
        use_case.execute(AskPortfolioRequest(question=question))


def test_rate_limit_prevents_agent_call() -> None:
    limiter = FakeRateLimiter(allow=False)
    use_case, agent, _, _ = make_use_case(rate_limiter=limiter)

    with pytest.raises(RateLimitExceeded):
        use_case.execute(AskPortfolioRequest(question="Question"))

    assert agent.calls == []


@pytest.mark.asyncio
async def test_async_execution_and_conversation_id() -> None:
    use_case, _, _, _ = make_use_case()

    result = await use_case.aexecute(
        AskPortfolioRequest(question="Question", conversation_id="conv-123")
    )

    assert result.conversation_id == "conv-123"


# ---------------------------------------------------------------------------
# Streaming path (PR2b /chat/stream wire): astream must surface tool calls.
# ---------------------------------------------------------------------------


class FakeStreamingAgent:
    """A fake AgentPort that yields a deterministic sequence of ``AgentChunk``s.

    Mirrors what ``LangChainAgentAdapter.stream`` will produce once the
    new ``tool_call`` surfacing lands: a tool_call chunk, then a
    token, then ``done``. The use case MUST yield one
    ``AskPortfolioChunk(kind="tool_call")`` and audit-warn for the
    tool name; the SSE encoder turns that into a typed event the
    browser renders as a trace pill.
    """

    def __init__(self, chunks: list[AgentChunk]) -> None:
        self.chunks = chunks
        self.run_calls: list[AgentRequest] = []
        self.stream_calls: list[AgentRequest] = []

    async def run(self, request: AgentRequest, tools: list[Any]) -> AgentResponse:
        self.run_calls.append(request)
        return AgentResponse(answer="unused")

    async def stream(self, request: AgentRequest, tools: list[Any]) -> AsyncIterator[AgentChunk]:
        self.stream_calls.append(request)
        for chunk in self.chunks:
            yield chunk


def make_streaming_use_case(
    agent: FakeStreamingAgent,
) -> tuple[AskPortfolioUseCase, FakeAudit, FakeRateLimiter]:
    audit = FakeAudit()
    limiter = FakeRateLimiter()
    use_case = AskPortfolioUseCase(
        agent=agent,
        tools=["tool"],
        sanitizer=OutputSanitizer(audit=audit),
        audit=audit,
        rate_limiter=limiter,
    )
    return use_case, audit, limiter


@pytest.mark.asyncio
async def test_astream_yields_tool_call_chunk_and_audits_tool_call() -> None:
    """``astream`` MUST yield one ``AskPortfolioChunk(kind='tool_call',
    tool_call=...)`` per agent tool_call chunk AND emit
    ``audit.warn('agent.tool_call', tool=<name>, source='ask_portfolio')``.

    The audit parity with ``aexecute`` (asserted in
    ``test_audits_tool_calls``) is non-negotiable: every tool call
    the agent makes — buffered or streaming — must show up in the
    security audit trail. The streaming chunk shape is the bridge
    the SSE encoder needs to forward a typed ``event: tool_call``
    line to the browser pill renderer.
    """
    agent = FakeStreamingAgent(
        chunks=[
            AgentChunk(
                kind="tool_call",
                data={"name": "search_code", "args": {"query": "rate limit"}, "id": "toolu_abc"},
            ),
            AgentChunk(kind="token", data="Found it."),
            AgentChunk(kind="done", data=""),
        ]
    )
    use_case, audit, _ = make_streaming_use_case(agent)

    yielded: list[AskPortfolioChunk] = []
    async for chunk in use_case.astream(
        AskPortfolioRequest(question="How does rate limiting work?")
    ):
        yielded.append(chunk)

    tool_call_chunks = [c for c in yielded if c.kind == "tool_call"]
    assert len(tool_call_chunks) == 1
    assert tool_call_chunks[0].tool_call == {
        "name": "search_code",
        "args": {"query": "rate limit"},
        "id": "toolu_abc",
    }
    # Audit parity with ``aexecute`` — the security trail records every tool call.
    assert ("agent.tool_call", {"tool": "search_code", "source": "ask_portfolio"}) in audit.events
    # The terminal done chunk MUST carry the tool name in tools_called.
    done_chunks = [c for c in yielded if c.kind == "done"]
    assert len(done_chunks) == 1
    assert done_chunks[0].result is not None
    assert done_chunks[0].result.tools_called == ["search_code"]


@pytest.mark.asyncio
async def test_astream_sanitizes_tokens_but_preserves_tool_call_intact() -> None:
    """Token chunks MUST be sanitized via ``OutputSanitizer``; tool_call
    chunks MUST pass through verbatim (the sanitizer is for prose —
    redaction rules don't apply to a structured tool dispatch).
    """
    agent = FakeStreamingAgent(
        chunks=[
            AgentChunk(kind="tool_call", data={"name": "list_projects", "args": {}, "id": "t1"}),
            AgentChunk(kind="token", data="Key: AKIAIOSFODNN7EXAMPLE"),
            AgentChunk(kind="done", data=""),
        ]
    )
    use_case, _, _ = make_streaming_use_case(agent)

    yielded: list[AskPortfolioChunk] = []
    async for chunk in use_case.astream(AskPortfolioRequest(question="List projects")):
        yielded.append(chunk)

    tokens = [c for c in yielded if c.kind == "token"]
    expected_redacted = "Key: " + "[REDACTED]"
    assert tokens[0].answer_token == expected_redacted
    # Tool-call payload MUST round-trip — the renderer needs the
    # original dict to extract name + args for the pill.
    tool_calls = [c for c in yielded if c.kind == "tool_call"]
    assert tool_calls[0].tool_call == {"name": "list_projects", "args": {}, "id": "t1"}
