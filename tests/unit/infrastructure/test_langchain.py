from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mcp_server.application.ports.agent import AgentRequest
from mcp_server.infrastructure.langchain import LangChainAgentAdapter, LangChainChunkingAdapter


def test_python_chunking_preserves_offsets() -> None:
    content = "def first():\n    return 1\n\n\ndef second():\n    return 2\n"
    adapter = LangChainChunkingAdapter(chunk_size=35, chunk_overlap=5)

    chunks = adapter.chunk(content, Path("example.py"))

    assert len(chunks) >= 2
    assert all(content[chunk.start_char : chunk.end_char] == chunk.text for chunk in chunks)


def test_markdown_chunking_uses_heading_boundaries() -> None:
    content = "# First\n\nAlpha paragraph.\n\n# Second\n\nBeta paragraph."
    adapter = LangChainChunkingAdapter(chunk_size=30, chunk_overlap=0)

    chunks = adapter.chunk(content, Path("README.md"))

    assert [chunk.text for chunk in chunks] == [
        "# First\n\nAlpha paragraph.",
        "# Second\n\nBeta paragraph.",
    ]


def test_empty_content_returns_no_chunks() -> None:
    adapter = LangChainChunkingAdapter()

    assert adapter.chunk("", Path("empty.txt")) == []


@pytest.mark.asyncio
async def test_agent_unwraps_tools_and_extracts_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeAgent:
        async def ainvoke(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
            captured["payload"] = payload
            captured["config"] = config
            return {
                "messages": [
                    SimpleNamespace(content="", tool_calls=[{"name": "search_code"}]),
                    SimpleNamespace(content="portfolio answer", tool_calls=[]),
                ]
            }

    def fake_create_react_agent(llm: Any, tools: list[Any]) -> FakeAgent:
        captured["tools"] = tools
        return FakeAgent()

    monkeypatch.setattr(
        "mcp_server.infrastructure.langchain.create_react_agent",
        fake_create_react_agent,
    )
    tool_function = lambda: None
    adapter = LangChainAgentAdapter(api_key="test", llm=object())

    response = await adapter.run(
        AgentRequest(question="Which project?", max_tool_calls=3),
        [SimpleNamespace(fn=tool_function)],
    )

    assert captured["tools"] == [tool_function]
    assert captured["config"] == {"recursion_limit": 7}
    assert response.answer == "portfolio answer"
    assert response.tool_calls == [{"name": "search_code"}]
