"""``AskPortfolioUseCase`` — application-layer ``ask_portfolio`` MCP tool.

The use case is the **meta-tool** for 002-mcp-tools PR3. It exposes a
Pydantic AI ``Agent`` (built once in :func:`mcp_server.composition.compose`)
that has the other 5 sibling MCP tools registered as function-calling
tools. A recruiter asks an open-ended question and the agent decides
which sibling tools to call before producing a final ``answer``.

Pipeline (per ``execute()`` call):

1. **Layer 5 pre-check** — call :meth:`RateLimiterPort.check(client_ip)`.
   The agent is the expensive endpoint (a 5-tool-call loop against
   Gemini is several cents per request); the application-layer check
   is belt-and-braces against a future router refactor that forgets
   the slowapi exception handler.
2. **Validate input** — empty / whitespace-only ``question`` raises
   :class:`ValueError` (mapped to JSON-RPC ``-32602`` by the wrapper).
3. **Run the agent** — :meth:`Agent.run_sync` drives the
   Pydantic AI tool loop up to ``max_tool_calls=5`` rounds
   (ADR-001 follow-up R3: cap Pydantic AI loops).
4. **Layer 3 sanitization** — the agent's final ``answer`` is the
   highest-risk redaction surface (it concatenates output from
   multiple sibling tools), so it passes through
   :meth:`OutputSanitizer.sanitize` before serialization.
5. **Audit trail** — each tool the agent calls emits
   ``audit.warn("agent.tool_call", tool=...)`` for replay.

Hexagonal contract
------------------

Depends on the Pydantic AI ``Agent`` instance built by the composition
root + framework-free dependencies:

* :class:`mcp_server.application.ports.rate_limiter.RateLimiterPort`
* :class:`mcp_server.security.output_sanitizer.OutputSanitizer`
* :class:`mcp_server.security.audit.AuditLogger`

No concrete adapter imports. The Pydantic AI ``Agent`` is built in
``composition.compose()`` (the only wiring point) and injected via
the constructor — the use case never imports ``pydantic_ai`` directly.

Why ``run_sync`` (not ``run``)?
-------------------------------

The use case is invoked from the async ``ask_portfolio_tool`` wrapper
which is itself called by FastMCP's async tool dispatcher. The
Pydantic AI ``Agent.run_sync`` helper uses ``asyncio.run`` internally
which raises ``RuntimeError`` when an event loop is already running.
We therefore detect whether we're inside a running loop and choose
between ``agent.run()`` (async, awaited) and ``agent.run_sync()``
(blocking). Both are no-ops at the agent's API surface; the choice
only affects the event-loop integration.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from mcp_server.application.ports.rate_limiter import RateLimiterPort
from mcp_server.domain.exceptions import RateLimitExceeded
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer

#: Default cap on Pydantic AI multi-step tool-call loops. ADR-001
#: follow-up: ``max_tool_calls=5`` bounds per-call cost (Gemini
#: ~700 ms/round on free tier → worst-case ~3.5 s).
DEFAULT_MAX_TOOL_CALLS: int = 5

#: The default client IP for application-layer rate-limit checks when
#: the caller does not provide one. Mirrors the test-suite convention
#: (``127.0.0.1``).
DEFAULT_CLIENT_IP: str = "127.0.0.1"

__all__ = [
    "DEFAULT_CLIENT_IP",
    "DEFAULT_MAX_TOOL_CALLS",
    "AskPortfolioRequest",
    "AskPortfolioResult",
    "AskPortfolioUseCase",
]


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AskPortfolioRequest:
    """Inputs to :meth:`AskPortfolioUseCase.execute`.

    Attributes:
        question: Natural-language recruiter question. MUST be non-empty
            after ``.strip()``.
        conversation_id: Reserved for future multi-turn conversations.
            Echoed back in the result; not used to drive state in PR3.
        client_ip: Client IP for the application-layer rate-limit check.
            Defaults to :data:`DEFAULT_CLIENT_IP` (``"127.0.0.1"``) so
            unit tests can omit it; production callers MUST forward
            ``request.client.host``.
    """

    question: str
    conversation_id: str | None = None
    client_ip: str = DEFAULT_CLIENT_IP


@dataclass(frozen=True)
class AskPortfolioResult:
    """Output of :meth:`AskPortfolioUseCase.execute`.

    Attributes:
        answer: Sanitized recruiter-facing reply (Layer 3).
        tools_called: Audit trail of the sibling tools the agent
            invoked (e.g. ``["list_projects_tool", "search_code_tool"]``).
            Empty list when the agent answered without calling any
            sibling tool.
        conversation_id: Echoed back from the request (or ``None``).
    """

    answer: str
    tools_called: list[str] = field(default_factory=list)
    conversation_id: str | None = None


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


# Module-level imports kept narrow so the domain stays framework-free
# (Pydantic AI is imported at runtime, only when the use case runs).
try:
    from pydantic_ai import Agent as _Agent
    from pydantic_ai.exceptions import UsageLimitExceeded as _UsageLimitExceeded
    from pydantic_ai.messages import ToolCallPart as _ToolCallPart
except ImportError:  # pragma: no cover — pydantic-ai is a hard dep
    _Agent = None  # type: ignore[assignment]
    _UsageLimitExceeded = None  # type: ignore[assignment]
    _ToolCallPart = None  # type: ignore[assignment]


class AskPortfolioUseCase:
    """Drive the Pydantic AI ``Agent`` and sanitize the resulting ``answer``.

    Args:
        agent: Pydantic AI :class:`Agent` instance built by the
            composition root. Pre-loaded with the 5 sibling MCP tools
            as function-calling tools (see ADR-001).
        sanitizer: :class:`OutputSanitizer` — Layer 3. Applied to the
            agent's final ``answer`` to redact secrets that leaked
            through aggregated sibling-tool output.
        audit: :class:`AuditLogger` — Layer 5. Emits one
            ``agent.tool_call`` event per sibling tool the agent calls.
        rate_limiter: :class:`RateLimiterPort` — Layer 5 application-
            layer check. Raises :class:`RateLimitExceeded` when the
            configured quota is exhausted.
        max_tool_calls: Cap on the Pydantic AI multi-step tool-call
            loop (default :data:`DEFAULT_MAX_TOOL_CALLS`). Passed to
            the agent's ``usage_limits(tool_calls_limit=...)`` knob.
    """

    def __init__(
        self,
        *,
        agent: Any,
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
        rate_limiter: RateLimiterPort,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    ) -> None:
        if agent is None:
            # Defensive — composition root MUST inject a real agent.
            raise RuntimeError(
                "AskPortfolioUseCase requires a Pydantic AI Agent instance "
                "built by composition.compose(); received None"
            )
        self.agent = agent
        self.sanitizer = sanitizer
        self.audit = audit
        self.rate_limiter = rate_limiter
        self.max_tool_calls = max_tool_calls

    def execute(self, request: AskPortfolioRequest) -> AskPortfolioResult:
        """Synchronous entry point — runs the agent via ``run_sync``.

        This works when the caller is NOT inside an asyncio event loop
        (e.g. unit tests, CLI helpers). When called from inside a
        running loop (e.g. the FastMCP async tool dispatcher), use
        :meth:`aexecute` instead.
        """
        return asyncio.run(self.aexecute(request))

    async def aexecute(self, request: AskPortfolioRequest) -> AskPortfolioResult:
        """Async entry point — runs the agent via ``run`` (awaited).

        Use this when the caller is already inside an event loop
        (typical for the FastMCP async tool dispatcher). Falls back to
        :meth:`execute` from sync contexts.

        Args:
            request: Inputs — see :class:`AskPortfolioRequest`.

        Returns:
            :class:`AskPortfolioResult` with the sanitized ``answer``
            and the audit trail of tool calls.

        Raises:
            ValueError: ``question`` is empty / whitespace-only (mapped
                to JSON-RPC ``-32602``).
            RateLimitExceeded: the application-layer rate-limit check
                rejected the request (mapped to JSON-RPC ``-32603``).
            McpServerError: the Pydantic AI agent tripped the
                ``tool_calls_limit`` cap or any other domain-class
                exception (the MCP wrapper translates via
                :func:`translate_tool_error`).
        """
        # 1. Layer 5 application-layer rate-limit pre-check.
        if not self.rate_limiter.check(request.client_ip):
            raise RateLimitExceeded(
                f"rate limit exceeded for client_ip={request.client_ip}"
            )

        # 2. Input validation. The wrapper maps ValueError -> -32602.
        if not request.question or not request.question.strip():
            raise ValueError("question must be a non-empty, non-whitespace string")

        # 3. Run the agent. ``usage_limits(tool_calls_limit=...)`` is
        # the Pydantic AI knob that bounds the multi-step tool-call
        # loop (ADR-001). We catch the resulting exception and
        # re-raise as a domain-class so the MCP wrapper can map it.
        from pydantic_ai.exceptions import UsageLimitExceeded
        from pydantic_ai.usage import UsageLimits

        run_coroutine = self.agent.run(
            request.question,
            usage_limits=UsageLimits(tool_calls_limit=self.max_tool_calls),
        )
        try:
            run_result = await run_coroutine
        except UsageLimitExceeded as exc:
            # Re-raise as a domain-class exception so the wrapper
            # translates it via translate_tool_error (DomainError catch-all).
            # We deliberately do NOT echo the Pydantic AI "tool_calls=N"
            # message — it's authored text but the recruiter-friendly
            # wording is preferred.
            self.audit.warn(
                "agent.max_tool_calls_exceeded",
                max_tool_calls=self.max_tool_calls,
            )
            from mcp_server.domain.exceptions import McpServerError

            raise McpServerError(
                "the agent needed more tools than allowed to answer this question — "
                "try a narrower prompt"
            ) from exc

        # 4. Extract the final answer and the tool-call audit trail.
        answer_text = self._extract_answer(run_result)
        tools_called = self._extract_tool_calls(run_result)

        # Emit one audit event per sibling tool the agent called.
        for tool_name in tools_called:
            self.audit.warn("agent.tool_call", tool=tool_name, source="ask_portfolio")

        # 5. Layer 3 — sanitize the final answer.
        sanitized = self.sanitizer.sanitize(answer_text, source="ask_portfolio")

        return AskPortfolioResult(
            answer=sanitized.redacted_text,
            tools_called=tools_called,
            conversation_id=request.conversation_id,
        )

    # ------------------------------------------------------------------
    # Helpers — keep them tiny; pure functions are easier to test.
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_answer(run_result: Any) -> str:
        """Return the final text answer from a Pydantic AI ``AgentRunResult``.

        The agent's ``output`` attribute carries the validated final
        output (string when ``output_type=str``). Falling back to
        ``str(output)`` defends against non-string output types.
        """
        return getattr(run_result, "output", str(run_result))

    @staticmethod
    def _extract_tool_calls(run_result: Any) -> list[str]:
        """Return the ordered list of tool names the agent invoked.

        Walks ``run_result.all_messages()`` for ``ModelResponse`` entries
        containing :class:`ToolCallPart` and collects the ``tool_name``
        attribute of each. Returns an empty list when the agent answered
        without invoking any sibling tool.
        """
        from pydantic_ai.messages import ModelResponse, ToolCallPart

        tools: list[str] = []
        all_messages = getattr(run_result, "all_messages", None)
        if not callable(all_messages):
            return tools
        for message in all_messages():
            if not isinstance(message, ModelResponse):
                continue
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    tools.append(part.tool_name)
        return tools
