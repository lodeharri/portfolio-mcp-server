"""Unit tests for ``AskPortfolioUseCase`` (002-mcp-tools PR3).

The use case is the meta-tool: a Pydantic AI ``Agent`` that has the 5
sibling MCP tools as function-calling tools. A recruiter asks an
open-ended question and the agent decides which sibling tools to call
before producing a final ``answer``.

The tests use :class:`pydantic_ai.models.function.FunctionModel` so
the agent's behavior is fully deterministic — no real LLM calls,
no network I/O. Each test feeds the model a scripted response and
asserts on the resulting use case output.

Hexagonal contract
------------------

Depends ONLY on ports + framework-free dependencies:

* :class:`mcp_server.application.ports.rate_limiter.RateLimiterPort`
* :class:`mcp_server.security.output_sanitizer.OutputSanitizer`
* :class:`mcp_server.security.audit.AuditLogger`

No concrete adapter imports. The Pydantic AI ``Agent`` is built in
``composition.compose()`` (the only wiring point) and injected via the
constructor — the use case never imports ``pydantic_ai`` directly.

Why mock the agent?
-------------------

The use case depends on a fully-built ``Agent`` instance. Building a
real ``Agent`` against the live Gemini API would make these tests
flaky (network) and slow (1-5 LLM rounds). A :class:`FunctionModel`
scripted response makes the tests deterministic — the same input
produces the same output every run.

The trade-off: we lose coverage of the actual Pydantic AI tool-loop
plumbing. That is exercised in
``tests/integration/test_agent_registers_sibling_tools.py`` (5-tool
composition) and ``tests/integration/test_mcp_tools_ask_portfolio.py``
(end-to-end smoke under ``--mock-gemini``).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from mcp_server.application.use_cases.ask_portfolio import (
    AskPortfolioRequest,
    AskPortfolioResult,
    AskPortfolioUseCase,
    DEFAULT_MAX_TOOL_CALLS,
)
from mcp_server.domain.exceptions import RateLimitExceeded
from mcp_server.security.output_sanitizer import OutputSanitizer

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeRateLimiter:
    """In-memory :class:`RateLimiterPort` fake.

    ``allow`` is consulted once per ``check()`` call. When ``False``,
    ``check()`` returns ``False`` (the use case raises
    :class:`RateLimitExceeded`).
    """

    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[str] = []

    def check(self, client_ip: str) -> bool:
        self.calls.append(client_ip)
        return self.allow

    def limit(self) -> str:
        return "30/minute"


class _FakeAudit:
    """In-memory audit logger for tests."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warn(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def _build_agent_with_script(
    responses: list[ModelResponse],
) -> tuple[Agent, list[int]]:
    """Build an :class:`Agent` whose model emits a scripted sequence of responses.

    The model function pops from ``responses`` (FIFO) on every call —
    deterministic across runs.

    Args:
        responses: Sequence of :class:`ModelResponse` objects the agent
            will receive in order. Each one corresponds to one model
            invocation (LLM round).

    Returns:
        ``(agent, call_log)`` — call_log is appended with the round
        index every time the model is invoked.
    """
    call_log: list[int] = []
    queue = list(responses)

    def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        call_log.append(len(call_log))
        return queue.pop(0)

    return (
        Agent(model=FunctionModel(model_fn), output_type=str),
        call_log,
    )


def _make_use_case(
    *,
    agent: Agent | None = None,
    sanitizer: OutputSanitizer | None = None,
    audit: _FakeAudit | None = None,
    rate_limiter: _FakeRateLimiter | None = None,
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
) -> tuple[AskPortfolioUseCase, _FakeAudit, _FakeRateLimiter, OutputSanitizer]:
    """Build a :class:`AskPortfolioUseCase` with sensible defaults."""
    if agent is None:
        agent, _ = _build_agent_with_script(
            [ModelResponse(parts=[TextPart("clean answer")])]
        )
    if audit is None:
        audit = _FakeAudit()
    if sanitizer is None:
        sanitizer = OutputSanitizer(audit=audit)
    if rate_limiter is None:
        rate_limiter = _FakeRateLimiter()
    use_case = AskPortfolioUseCase(
        agent=agent,
        sanitizer=sanitizer,
        audit=audit,
        rate_limiter=rate_limiter,
        max_tool_calls=max_tool_calls,
    )
    return use_case, audit, rate_limiter, sanitizer


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestAskPortfolioHappyPath:
    """A clean question produces a sanitized answer and the call log."""

    def test_returns_sanitized_answer_with_no_incidents(self) -> None:
        """A clean answer passes through the sanitizer with no redacted text."""
        use_case, audit, rate_limiter, _ = _make_use_case()

        result = use_case.execute(AskPortfolioRequest(question="Which project is closest to production?"))

        assert isinstance(result, AskPortfolioResult)
        assert result.answer == "clean answer"
        assert result.tools_called == []
        # Rate limiter was consulted exactly once.
        assert rate_limiter.calls == ["127.0.0.1"]
        # No redactions → no audit events from the sanitizer.
        assert not [e for e in audit.events if e[0] == "output.redacted"]

    def test_empty_question_raises_value_error(self) -> None:
        """Empty / whitespace-only questions MUST raise ``ValueError``."""
        use_case, _, _, _ = _make_use_case()

        with pytest.raises(ValueError, match="non-empty"):
            use_case.execute(AskPortfolioRequest(question=""))

        with pytest.raises(ValueError, match="non-empty"):
            use_case.execute(AskPortfolioRequest(question="   "))


# ---------------------------------------------------------------------------
# Tool-call audit trail
# ---------------------------------------------------------------------------


class TestAskPortfolioToolCallAudit:
    """Every tool the agent calls is recorded in the audit log."""

    def test_tool_call_appears_in_audit_with_tool_name(self) -> None:
        """When the agent requests ``list_projects``, an ``agent.tool_call``
        audit event is emitted with the tool name."""
        # Build a FunctionModel that requests ``list_projects`` once
        # then yields a final text answer.
        agent, calls = _build_agent_with_script(
            [
                ModelResponse(
                    parts=[ToolCallPart(tool_name="list_projects_tool", args={})]
                ),
                ModelResponse(parts=[TextPart("final")]),
            ]
        )
        # Register a sibling tool that the model can actually invoke.
        async def list_projects_tool() -> list[dict[str, Any]]:
            return [{"id": "demo", "display_name": "Demo"}]

        agent.tool_plain(list_projects_tool)  # type: ignore[arg-type]
        use_case, audit, _, _ = _make_use_case(agent=agent)

        result = use_case.execute(AskPortfolioRequest(question="list them"))

        # Two model invocations (one tool request + one final answer).
        assert calls == [0, 1]
        # The tool name appears in the audit trail.
        tool_events = [e for e in audit.events if e[0] == "agent.tool_call"]
        assert len(tool_events) == 1
        assert tool_events[0][1]["tool"] == "list_projects_tool"
        # tools_called reflects what the agent invoked.
        assert "list_projects_tool" in result.tools_called


# ---------------------------------------------------------------------------
# Sanitization (Layer 3)
# ---------------------------------------------------------------------------


class TestAskPortfolioSanitization:
    """The agent's final ``answer`` is sanitized via :class:`OutputSanitizer`."""

    def test_aws_key_in_answer_is_redacted(self) -> None:
        """An AWS-shaped key in the agent's answer is replaced with ``[REDACTED]``."""
        agent, _ = _build_agent_with_script(
            [ModelResponse(parts=[TextPart("The key is AKIAIOSFODNN7EXAMPLE for real")])]
        )
        use_case, audit, _, _ = _make_use_case(agent=agent)

        result = use_case.execute(AskPortfolioRequest(question="any"))

        assert "[REDACTED]" in result.answer
        assert "AKIAIOSFODNN7EXAMPLE" not in result.answer
        # Audit emits ``output.redacted`` for the sanitized source.
        redacted_events = [e for e in audit.events if e[0] == "output.redacted"]
        assert any(e[1].get("source") == "ask_portfolio" for e in redacted_events)

    def test_github_pat_in_answer_is_redacted(self) -> None:
        """A GitHub PAT in the agent's answer is redacted."""
        agent, _ = _build_agent_with_script(
            [ModelResponse(parts=[TextPart("the token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789ab is bad")])]
        )
        use_case, audit, _, _ = _make_use_case(agent=agent)

        result = use_case.execute(AskPortfolioRequest(question="any"))

        assert "[REDACTED]" in result.answer
        assert "ghp_" not in result.answer

    def test_clean_answer_passes_through_unchanged(self) -> None:
        """A clean answer returns verbatim with no redaction incidents."""
        use_case, audit, _, _ = _make_use_case()

        result = use_case.execute(AskPortfolioRequest(question="any"))

        assert result.answer == "clean answer"
        assert not [e for e in audit.events if e[0] == "output.redacted"]


# ---------------------------------------------------------------------------
# Rate limiter (Layer 5 application-layer pre-check)
# ---------------------------------------------------------------------------


class TestAskPortfolioRateLimit:
    """A failing rate limiter check raises :class:`RateLimitExceeded`."""

    def test_denied_request_raises_rate_limit_exceeded(self) -> None:
        use_case, _, _, _ = _make_use_case(rate_limiter=_FakeRateLimiter(allow=False))

        with pytest.raises(RateLimitExceeded):
            use_case.execute(AskPortfolioRequest(question="hi"))

    def test_denied_request_does_not_invoke_agent(self) -> None:
        """When the limiter denies, the agent MUST NOT run (cost guard)."""
        agent, calls = _build_agent_with_script(
            [ModelResponse(parts=[TextPart("should not run")])]
        )
        use_case, _, _, _ = _make_use_case(
            agent=agent, rate_limiter=_FakeRateLimiter(allow=False)
        )

        with pytest.raises(RateLimitExceeded):
            use_case.execute(AskPortfolioRequest(question="hi"))

        # Agent was never invoked.
        assert calls == []


# ---------------------------------------------------------------------------
# max_tool_calls cap
# ---------------------------------------------------------------------------


class TestAskPortfolioMaxToolCalls:
    """The Pydantic AI ``usage_limits(tool_calls_limit=...)`` is enforced.

    When the agent exceeds the cap, Pydantic AI raises
    :class:`pydantic_ai.exceptions.UsageLimitExceeded`. The use case
    re-raises it as a recruiter-friendly :class:`DomainError`-derived
    exception so the MCP layer can translate it.
    """

    def test_runaway_loop_aborts_with_usage_limit_exceeded(self) -> None:
        """A FunctionModel that always requests a tool call eventually trips
        the ``tool_calls_limit`` cap and raises a domain-class exception."""
        # Always request a tool call — never yields a final answer.
        agent, _ = _build_agent_with_script(
            [
                ModelResponse(parts=[ToolCallPart(tool_name="my_tool", args={})])
                for _ in range(DEFAULT_MAX_TOOL_CALLS + 5)
            ]
        )
        async def my_tool() -> str:
            return "ok"

        agent.tool_plain(my_tool)  # type: ignore[arg-type]
        use_case, _, _, _ = _make_use_case(agent=agent)

        with pytest.raises(Exception) as exc_info:
            use_case.execute(AskPortfolioRequest(question="loop forever"))

        # The exception must be domain-derived (McpServerError family)
        # so translate_tool_error catches it.
        from mcp_server.domain.exceptions import McpServerError

        assert isinstance(exc_info.value, McpServerError)

    def test_default_max_tool_calls_is_five(self) -> None:
        """The default cap is 5 (ADR-005 follow-up)."""
        assert DEFAULT_MAX_TOOL_CALLS == 5


# ---------------------------------------------------------------------------
# Conversation id echo
# ---------------------------------------------------------------------------


class TestAskPortfolioConversationId:
    """The ``conversation_id`` is echoed back when provided."""

    def test_conversation_id_is_echoed_in_result(self) -> None:
        use_case, _, _, _ = _make_use_case()

        result = use_case.execute(
            AskPortfolioRequest(
                question="hi",
                conversation_id="conv-123",
            )
        )

        assert result.conversation_id == "conv-123"

    def test_no_conversation_id_returns_none(self) -> None:
        use_case, _, _, _ = _make_use_case()

        result = use_case.execute(AskPortfolioRequest(question="hi"))

        assert result.conversation_id is None


# ---------------------------------------------------------------------------
# Run via asyncio (the use case may be called from async contexts)
# ---------------------------------------------------------------------------


def test_use_case_executes_via_aexecute() -> None:
    """Calling the use case via :meth:`aexecute` from an event loop works.

    The MCP tool wrapper runs inside an event loop; this test exercises
    that path directly.
    """
    use_case, _, _, _ = _make_use_case()

    async def call() -> AskPortfolioResult:
        return await use_case.aexecute(AskPortfolioRequest(question="async"))

    result = asyncio.run(call())
    assert result.answer == "clean answer"


def test_use_case_executes_via_sync_execute() -> None:
    """Calling the use case via :meth:`execute` from a sync context works.

    The sync entry point spins up its own asyncio loop internally
    (``asyncio.run``) — fine for unit tests and CLI helpers.
    """
    use_case, _, _, _ = _make_use_case()

    result = use_case.execute(AskPortfolioRequest(question="sync"))

    assert result.answer == "clean answer"
