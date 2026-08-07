"""Application orchestration for the ``ask_portfolio`` MCP tool."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp_server.application.ports.agent import AgentPort, AgentRequest
from mcp_server.application.ports.rate_limiter import RateLimiterPort
from mcp_server.domain.exceptions import RateLimitExceeded
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer

DEFAULT_MAX_TOOL_CALLS = 3
DEFAULT_CLIENT_IP = "127.0.0.1"

__all__ = [
    "DEFAULT_CLIENT_IP",
    "DEFAULT_MAX_TOOL_CALLS",
    "AskPortfolioChunk",
    "AskPortfolioRequest",
    "AskPortfolioResult",
    "AskPortfolioUseCase",
]


@dataclass(frozen=True)
class AskPortfolioRequest:
    question: str
    conversation_id: str | None = None
    client_ip: str = DEFAULT_CLIENT_IP


@dataclass(frozen=True)
class AskPortfolioResult:
    answer: str
    tools_called: list[str] = field(default_factory=list)
    conversation_id: str | None = None


@dataclass(frozen=True)
class AskPortfolioChunk:
    """A single event in the ``ask_portfolio`` streaming response.

    Per the 003-playground-ui agent-streaming spec:

    * ``kind='token'`` carries ``answer_token`` — one sanitized chunk
      of LLM prose.
    * ``kind='tool_call'`` carries ``tool_call`` — a sibling-tool
      dispatch notification.
    * ``kind='done'`` carries ``result`` — the final
      ``AskPortfolioResult`` with concatenated sanitized answer,
      tool list, and conversation id.
    * ``kind='error'`` (REL-3) carries ``error`` — the stringified
      exception caught mid-stream. The SSE layer formats this as
      ``data: [ERROR]\\n\\n``.

    Exactly one field per kind is populated (the others are ``None``).
    """

    kind: Literal["token", "tool_call", "done", "error"]
    answer_token: str | None = None
    tool_call: dict[str, Any] | None = None
    result: AskPortfolioResult | None = None
    error: str | None = None


class AskPortfolioUseCase:
    """Run an injected agent and apply security boundaries to its response."""

    def __init__(
        self,
        *,
        agent: AgentPort,
        tools: list[Any],
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
        rate_limiter: RateLimiterPort,
        max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS,
    ) -> None:
        self.agent = agent
        self.tools = tools
        self.sanitizer = sanitizer
        self.audit = audit
        self.rate_limiter = rate_limiter
        self.max_tool_calls = max_tool_calls

    def execute(self, request: AskPortfolioRequest) -> AskPortfolioResult:
        return asyncio.run(self.aexecute(request))

    async def aexecute(self, request: AskPortfolioRequest) -> AskPortfolioResult:
        if not self.rate_limiter.check(request.client_ip):
            raise RateLimitExceeded(f"rate limit exceeded for client_ip={request.client_ip}")
        if not request.question or not request.question.strip():
            raise ValueError("question must be a non-empty, non-whitespace string")

        response = await self.agent.run(
            AgentRequest(
                question=request.question,
                max_tool_calls=self.max_tool_calls,
            ),
            self.tools,
        )
        tools_called = [
            str(tool_call.get("name")) for tool_call in response.tool_calls if tool_call.get("name")
        ]
        for tool_name in tools_called:
            self.audit.warn("agent.tool_call", tool=tool_name, source="ask_portfolio")

        sanitized = self.sanitizer.sanitize(response.answer, source="ask_portfolio")
        return AskPortfolioResult(
            answer=sanitized.redacted_text,
            tools_called=tools_called,
            conversation_id=request.conversation_id,
        )

    async def astream(self, request: AskPortfolioRequest) -> AsyncIterator[AskPortfolioChunk]:
        """Stream the agent's tokens, sanitizing each before yielding.

        Per the agent-streaming spec (003-playground-ui):

        * Rate-limit gate fires exactly once before the agent iterates.
        * Empty questions raise ``ValueError`` (parity with ``aexecute``).
        * Every token chunk is sanitized via
          ``self.sanitizer.sanitize(token, source="ask_portfolio")``
          BEFORE being yielded — the per-token Layer 3 invariant
          (ADR-005: the middleware buffers full bodies and cannot
          catch SSE bytes).
        * ``tool_call`` chunks emit one ``audit.warn("agent.tool_call",
          ...)`` event, mirroring ``aexecute``.
        * The terminal ``done`` chunk carries the
          ``AskPortfolioResult`` whose ``answer`` is the concatenation
          of all sanitized tokens and whose ``tools_called`` /
          ``conversation_id`` are populated.
        * A mid-stream exception (REL-3) becomes a terminal
          ``error`` chunk carrying the stringified exception and the
          stream ends — no partial ``AskPortfolioResult`` is yielded.
          ``tool.completed`` audit event is NOT emitted on this path.
        """
        if not self.rate_limiter.check(request.client_ip):
            raise RateLimitExceeded(f"rate limit exceeded for client_ip={request.client_ip}")
        if not request.question or not request.question.strip():
            raise ValueError("question must be a non-empty, non-whitespace string")

        accumulated: list[str] = []
        tools_called: list[str] = []
        agent_request = AgentRequest(
            question=request.question,
            max_tool_calls=self.max_tool_calls,
        )

        try:
            async for agent_chunk in self.agent.stream(agent_request, self.tools):
                if agent_chunk.kind == "token":
                    sanitized = self.sanitizer.sanitize(agent_chunk.data, source="ask_portfolio")
                    accumulated.append(sanitized.redacted_text)
                    yield AskPortfolioChunk(kind="token", answer_token=sanitized.redacted_text)
                elif agent_chunk.kind == "tool_call":
                    tool_call: dict[str, Any]
                    if isinstance(agent_chunk.data, dict):
                        tool_call = agent_chunk.data
                    else:
                        tool_call = {"name": str(agent_chunk.data)}
                    tool_name = str(tool_call.get("name", ""))
                    if tool_name:
                        tools_called.append(tool_name)
                        self.audit.warn(
                            "agent.tool_call",
                            tool=tool_name,
                            source="ask_portfolio",
                        )
                    yield AskPortfolioChunk(kind="tool_call", tool_call=tool_call)
                elif agent_chunk.kind == "done":
                    result = AskPortfolioResult(
                        answer="".join(accumulated),
                        tools_called=tools_called,
                        conversation_id=request.conversation_id,
                    )
                    self.audit.info(
                        "tool.completed",
                        source="ask_portfolio",
                        tools_called=tools_called,
                    )
                    yield AskPortfolioChunk(kind="done", result=result)
                    return
                elif agent_chunk.kind == "error":
                    # Mid-stream agent-reported error — same REL-3 contract
                    # as a raised exception below.
                    yield AskPortfolioChunk(kind="error", error=str(agent_chunk.data))
                    return
        except Exception as exc:
            # REL-3: SSE layer renders this as ``data: [ERROR]\\n\\n``.
            # Broad catch is intentional — this is the boundary between
            # the agent adapter's async generator and the SSE encoder;
            # any exception raised by the upstream LangGraph call must
            # become a terminal error chunk instead of propagating up
            # to FastAPI (which would return a 500).
            yield AskPortfolioChunk(kind="error", error=str(exc))
            return
