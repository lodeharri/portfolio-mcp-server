"""Application orchestration for the ``ask_portfolio`` MCP tool."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from mcp_server.application.ports.agent import AgentPort, AgentRequest
from mcp_server.application.ports.rate_limiter import RateLimiterPort
from mcp_server.domain.exceptions import RateLimitExceeded
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer

DEFAULT_MAX_TOOL_CALLS = 5
DEFAULT_CLIENT_IP = "127.0.0.1"

__all__ = [
    "DEFAULT_CLIENT_IP",
    "DEFAULT_MAX_TOOL_CALLS",
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
            str(tool_call.get("name"))
            for tool_call in response.tool_calls
            if tool_call.get("name")
        ]
        for tool_name in tools_called:
            self.audit.warn("agent.tool_call", tool=tool_name, source="ask_portfolio")

        sanitized = self.sanitizer.sanitize(response.answer, source="ask_portfolio")
        return AskPortfolioResult(
            answer=sanitized.redacted_text,
            tools_called=tools_called,
            conversation_id=request.conversation_id,
        )
