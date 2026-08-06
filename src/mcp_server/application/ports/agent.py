from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    question: str
    history: list[dict[str, Any]] | None = None
    max_tool_calls: int = 5


class AgentResponse(BaseModel):
    answer: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    conversation_id: str | None = None


class AgentChunk(BaseModel):
    """A single event in the agent's streaming response.

    ``kind`` is the closed set defined by the agent-streaming spec:
    ``"token"`` for incremental LLM tokens, ``"tool_call"`` for a
    sibling-tool dispatch, ``"done"`` for the terminal sentinel, and
    ``"error"`` for an exception caught mid-stream (REL-3 — the SSE
    layer translates this to ``data: [ERROR]\\n\\n``).

    ``data`` carries the payload: a token string for ``"token"``, a
    tool-call description for ``"tool_call"``, an empty string for
    ``"done"``, and the stringified exception for ``"error"``.
    """

    kind: Literal["token", "tool_call", "done", "error"]
    data: str | dict[str, Any] = ""


@runtime_checkable
class AgentPort(Protocol):
    """Strategy for orchestrating multiple tools via an LLM agent.

    Buffered (``run``) and streaming (``stream``) variants are both
    part of the contract. ``run`` is used by the MCP ``ask_portfolio``
    tool (final-answer semantics); ``stream`` powers the browser
    playground's ``/chat/stream`` SSE route in PR2b.
    """

    async def run(self, request: AgentRequest, tools: list[Any]) -> AgentResponse: ...

    async def stream(
        self, request: AgentRequest, tools: list[Any]
    ) -> AsyncIterator[AgentChunk]: ...
