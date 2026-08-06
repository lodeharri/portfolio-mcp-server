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


def test_audits_tool_calls() -> None:
    agent = FakeAgent(
        AgentResponse(answer="answer", tool_calls=[{"name": "search_code"}])
    )
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
