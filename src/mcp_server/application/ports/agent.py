from typing import Any, Protocol

from pydantic import BaseModel, Field


class AgentRequest(BaseModel):
    question: str
    history: list[dict[str, Any]] | None = None
    max_tool_calls: int = 5


class AgentResponse(BaseModel):
    answer: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    conversation_id: str | None = None


class AgentPort(Protocol):
    """Strategy for orchestrating multiple tools via an LLM agent."""

    async def run(self, request: AgentRequest, tools: list[Any]) -> AgentResponse: ...
