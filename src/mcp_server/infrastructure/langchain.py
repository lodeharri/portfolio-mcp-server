"""LangChain adapters for chunking and agent orchestration."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_text_splitters import (
    MarkdownTextSplitter,
    PythonCodeTextSplitter,
    RecursiveCharacterTextSplitter,
)
from langgraph.prebuilt import create_react_agent

from mcp_server.application.ports.agent import AgentRequest, AgentResponse
from mcp_server.application.ports.chunking import Chunk


class LangChainChunkingAdapter:
    """Split source text with language-aware LangChain splitters."""

    def __init__(self, chunk_size: int = 1500, chunk_overlap: int = 200) -> None:
        options = {
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "add_start_index": True,
        }
        self._markdown = MarkdownTextSplitter(**options)
        self._python = PythonCodeTextSplitter(**options)
        self._generic = RecursiveCharacterTextSplitter(**options)

    def chunk(self, content: str, file_path: Path) -> list[Chunk]:
        if not content:
            return []
        extension = file_path.suffix.lower()
        if extension in {".md", ".markdown"}:
            splitter = self._markdown
        elif extension == ".py":
            splitter = self._python
        else:
            splitter = self._generic
        documents = splitter.create_documents([content])
        return [
            Chunk(
                text=document.page_content,
                start_char=document.metadata["start_index"],
                end_char=document.metadata["start_index"] + len(document.page_content),
            )
            for document in documents
        ]


class _MockLangChainAgentAdapter:
    async def run(self, request: AgentRequest, tools: Sequence[Any]) -> AgentResponse:
        return AgentResponse(answer="[mock answer to: hi]")


class LangChainAgentAdapter:
    """Run sibling tools through a LangGraph ReAct agent."""

    def __init__(
        self,
        api_key: str,
        model: str = "gemini-2.0-flash",
        *,
        llm: Any | None = None,
    ) -> None:
        self._llm = llm or ChatGoogleGenerativeAI(model=model, api_key=api_key)

    async def run(self, request: AgentRequest, tools: Sequence[Any]) -> AgentResponse:
        tool_functions = [getattr(tool, "fn", tool) for tool in tools]
        agent = create_react_agent(self._llm, tool_functions)
        messages = [*(request.history or []), {"role": "user", "content": request.question}]
        result = await agent.ainvoke(
            {"messages": messages},
            config={"recursion_limit": request.max_tool_calls * 2 + 1},
        )
        result_messages = result["messages"]
        tool_calls = [
            tool_call
            for message in result_messages
            for tool_call in getattr(message, "tool_calls", [])
        ]
        return AgentResponse(
            answer=str(result_messages[-1].content),
            tool_calls=tool_calls,
        )


def create_langchain_adapter(
    api_key: str = "",
    chunk_size: int = 1500,
    chunk_overlap: int = 200,
) -> LangChainChunkingAdapter:
    return LangChainChunkingAdapter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)


def create_langchain_agent(
    api_key: str,
    model: str = "gemini-2.0-flash",
) -> LangChainAgentAdapter | _MockLangChainAgentAdapter:
    if not api_key.strip():
        return _MockLangChainAgentAdapter()
    return LangChainAgentAdapter(api_key=api_key, model=model)
