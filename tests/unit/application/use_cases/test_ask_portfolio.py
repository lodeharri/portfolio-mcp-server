from __future__ import annotations

from typing import Any

import pytest

from mcp_server.application.ports.agent import AgentRequest, AgentResponse
from mcp_server.application.use_cases.ask_portfolio import (
    DEFAULT_MAX_TOOL_CALLS,
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
