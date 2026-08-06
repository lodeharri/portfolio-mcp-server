"""Unit tests for ``LangChainAgentAdapter.stream`` — the real LangGraph adapter.

Per the 003-playground-ui agent-streaming spec, the real adapter MUST:

* Invoke ``agent.astream(input, config, stream_mode="messages")`` — the
  LangGraph call surface the spec mandates.
* Yield one ``AgentChunk(kind="token", data=str(content))`` for each
  ``AIMessageChunk`` event in the stream.
* Skip non-AI messages (HumanMessage, ToolMessage, etc.) so the chat
  shows only the assistant's prose.
* Pass ``recursion_limit=request.max_tool_calls * 2 + 1`` (same
  constant ``run`` uses).
* Yield a terminal ``AgentChunk(kind="done", data="")``.

These tests stub ``create_react_agent`` (the LangGraph factory) so no
network calls happen — they are unit tests, not integration tests.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage

from mcp_server.application.ports.agent import (
    AgentChunk,
    AgentRequest,
)
from mcp_server.infrastructure.langchain import (
    LangChainAgentAdapter,
    _MockLangChainAgentAdapter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubLangGraphAgent:
    """A stub LangGraph agent that yields a deterministic sequence of messages."""

    def __init__(self, messages: list[tuple[Any, dict[str, Any]]]) -> None:
        self._messages = messages
        self.astream_calls: list[dict[str, Any]] = []

    async def astream(
        self,
        payload: dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        stream_mode: str = "values",
    ) -> AsyncIterator[tuple[Any, dict[str, Any]]]:
        # Mirror the real LangGraph signature: yield (message, metadata)
        # tuples when stream_mode='messages'.
        self.astream_calls.append(
            {"payload": payload, "config": config, "stream_mode": stream_mode}
        )
        for message, meta in self._messages:
            yield message, meta


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_ai_messages() -> list[tuple[Any, dict[str, Any]]]:
    """A typical LangGraph 'messages' stream: Human → AI → Human → AI.

    Uses real LangChain message types so ``isinstance(message,
    AIMessageChunk)`` works inside the adapter (the adapter does a
    strict ``isinstance`` filter, not a duck-type check).
    """

    return [
        (HumanMessage(content="What projects exist?"), {"langgraph_node": "agent"}),
        (AIMessageChunk(content="Looking"), {"langgraph_node": "agent"}),
        (AIMessageChunk(content=" up"), {"langgraph_node": "agent"}),
        (
            ToolMessage(content="list_projects output", tool_call_id="t1"),
            {"langgraph_node": "tools"},
        ),
        (AIMessageChunk(content="Found 3 projects."), {"langgraph_node": "agent"}),
    ]


@pytest.fixture
def fake_create_react_agent(stub_ai_messages, monkeypatch):
    """Patch ``create_react_agent`` so the adapter uses the stub LangGraph agent."""

    captured: dict[str, Any] = {}

    def _factory(llm: Any, tools: list[Any]) -> _StubLangGraphAgent:
        captured["llm"] = llm
        captured["tools"] = list(tools)
        return _StubLangGraphAgent(stub_ai_messages)

    monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)
    return captured


# ---------------------------------------------------------------------------
# Real adapter — stream() invokes astream with stream_mode='messages'
# ---------------------------------------------------------------------------


class TestLangChainAgentAdapterStream:
    """``LangChainAgentAdapter.stream`` MUST honor the agent-streaming spec."""

    @pytest.mark.asyncio
    async def test_recursion_limit_and_stream_mode_captured(self, monkeypatch) -> None:
        """Both ``stream_mode='messages'`` and ``recursion_limit`` MUST be passed.

        Combined assertion: covers both kwarg requirements in one
        test (the previous split tests were duplicative).
        """
        captured: dict[str, Any] = {}

        class _CaptureAgent:
            async def astream(
                self,
                payload: dict[str, Any],
                config: dict[str, Any] | None = None,
                *,
                stream_mode: str = "values",
            ) -> AsyncIterator[tuple[Any, dict[str, Any]]]:
                captured["stream_mode"] = stream_mode
                captured["config"] = config
                captured["payload"] = payload
                if False:
                    yield None, {}  # pragma: no cover
                return
                yield  # pragma: no cover

        def _factory(llm: Any, tools: list[Any]) -> _CaptureAgent:
            return _CaptureAgent()

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())
        async for _ in adapter.stream(AgentRequest(question="hi", max_tool_calls=4), []):
            pass

        assert captured["stream_mode"] == "messages"
        assert captured["config"] == {"recursion_limit": 9}  # 4 * 2 + 1

    @pytest.mark.asyncio
    async def test_yields_one_token_chunk_per_ai_message(
        self, stub_ai_messages, monkeypatch
    ) -> None:
        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())

        recorded: dict[str, Any] = {}

        def _factory(llm: Any, tools: list[Any]) -> _StubLangGraphAgent:
            recorded["tools"] = list(tools)
            return _StubLangGraphAgent(stub_ai_messages)

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        chunks: list[AgentChunk] = []
        async for chunk in adapter.stream(AgentRequest(question="List projects"), []):
            chunks.append(chunk)

        token_chunks = [c for c in chunks if c.kind == "token"]
        # 3 AI messages → 3 token chunks; Human + Tool are filtered.
        assert [c.data for c in token_chunks] == ["Looking", " up", "Found 3 projects."]
        # Final terminal sentinel.
        assert chunks[-1].kind == "done"
        assert chunks[-1].data == ""

    @pytest.mark.asyncio
    async def test_yields_at_least_one_chunk_within_5s(self, monkeypatch) -> None:
        """Real path MUST yield ≥1 chunk within 5s (PR2a integration gate)."""

        def _factory(llm: Any, tools: list[Any]) -> _StubLangGraphAgent:
            return _StubLangGraphAgent(
                [(AIMessageChunk(content="token"), {"langgraph_node": "agent"})]
            )

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())

        async def collect_one_chunk() -> AgentChunk:
            async for chunk in adapter.stream(AgentRequest(question="hi"), []):
                return chunk

        start = time.monotonic()
        chunk = await asyncio.wait_for(collect_one_chunk(), timeout=5.0)
        elapsed = time.monotonic() - start

        assert chunk.kind == "token"
        assert chunk.data == "token"
        assert elapsed < 5.0

    @pytest.mark.asyncio
    async def test_filters_non_ai_messages(self, monkeypatch) -> None:
        """HumanMessage and ToolMessage MUST NOT become token chunks."""
        # Mixed stream with NO AI messages — expect zero token chunks.
        non_ai_messages = [
            (HumanMessage(content="hello"), {}),
            (ToolMessage(content="tool result", tool_call_id="t1"), {}),
            (HumanMessage(content="follow-up"), {}),
        ]

        def _factory(llm: Any, tools: list[Any]) -> _StubLangGraphAgent:
            return _StubLangGraphAgent(non_ai_messages)

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())

        chunks: list[AgentChunk] = []
        async for chunk in adapter.stream(AgentRequest(question="hi"), []):
            chunks.append(chunk)

        token_chunks = [c for c in chunks if c.kind == "token"]
        assert token_chunks == [], (
            f"non-AI messages must not yield token chunks; got: {[c.data for c in token_chunks]}"
        )
        # Terminal sentinel MUST still arrive even with zero tokens.
        assert chunks[-1].kind == "done"

    @pytest.mark.asyncio
    async def test_recursion_limit_is_max_tool_calls_times_2_plus_1(self, monkeypatch) -> None:
        """Recursion limit MUST equal ``request.max_tool_calls * 2 + 1``.

        Same formula as ``run`` (PR1 contract). Triangulation: vary
        ``max_tool_calls`` to confirm the formula scales linearly.
        """
        captured: list[int] = []

        class _CaptureAgent:
            async def astream(
                self,
                payload: dict[str, Any],
                config: dict[str, Any] | None = None,
                *,
                stream_mode: str = "values",
            ) -> AsyncIterator[tuple[Any, dict[str, Any]]]:
                captured.append(config["recursion_limit"])
                if False:
                    yield None, {}  # pragma: no cover
                return
                yield  # pragma: no cover

        def _factory(llm: Any, tools: list[Any]) -> _CaptureAgent:
            return _CaptureAgent()

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())
        for max_calls in (1, 5, 8):
            async for _ in adapter.stream(
                AgentRequest(question="hi", max_tool_calls=max_calls), []
            ):
                pass

        assert captured == [3, 11, 17]  # 1*2+1, 5*2+1, 8*2+1

    @pytest.mark.asyncio
    async def test_done_chunk_terminates_stream(self, monkeypatch) -> None:
        """A terminal ``AgentChunk(kind='done', data='')`` MUST be yielded last."""

        def _factory(llm: Any, tools: list[Any]) -> _StubLangGraphAgent:
            return _StubLangGraphAgent(
                [
                    (AIMessageChunk(content="hello"), {}),
                    (AIMessageChunk(content=" world"), {}),
                ]
            )

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())

        chunks: list[AgentChunk] = []
        async for chunk in adapter.stream(AgentRequest(question="hi"), []):
            chunks.append(chunk)

        assert chunks[-1] == AgentChunk(kind="done", data="")
        # Exactly one done chunk — never more.
        assert sum(1 for c in chunks if c.kind == "done") == 1

    @pytest.mark.asyncio
    async def test_filters_chunks_with_none_content(self, monkeypatch) -> None:
        """``REL-12`` — chunks with ``content is None`` MUST NOT yield ``'None'``.

        LangGraph occasionally emits ``AIMessageChunk(content=None)``
        during tool handoff. ``str(None)`` is ``'None'`` — which is
        nonsensical as a chat token. The adapter must drop those.
        """

        def _factory(llm: Any, tools: list[Any]) -> _StubLangGraphAgent:
            # ``AIMessageChunk(content=None)`` is rejected by pydantic
            # validation in LangChain — use ``model_construct`` to
            # build an instance that bypasses the validator (matches
            # what LangGraph actually emits on tool handoff).
            none_chunk = AIMessageChunk.model_construct(content=None, id="x")
            return _StubLangGraphAgent(
                [
                    (AIMessageChunk(content="hello"), {}),
                    (none_chunk, {}),
                    (AIMessageChunk(content=" world"), {}),
                ]
            )

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())

        chunks: list[AgentChunk] = []
        async for chunk in adapter.stream(AgentRequest(question="hi"), []):
            chunks.append(chunk)

        token_chunks = [c for c in chunks if c.kind == "token"]
        assert [c.data for c in token_chunks] == ["hello", " world"]
        # No chunk with the literal string 'None'.
        assert "None" not in [c.data for c in token_chunks]


# ---------------------------------------------------------------------------
# Mock adapter — same AgentChunk shape, zero network
# ---------------------------------------------------------------------------


class TestMockLangChainAgentAdapter:
    """The mock adapter MUST yield exactly the spec's 5 deterministic tokens."""

    @pytest.mark.asyncio
    async def test_yields_five_deterministic_tokens_then_done(self) -> None:
        mock = _MockLangChainAgentAdapter()

        chunks: list[AgentChunk] = []
        async for chunk in mock.stream(AgentRequest(question="hi"), []):
            chunks.append(chunk)

        token_chunks = [c for c in chunks if c.kind == "token"]
        assert [c.data for c in token_chunks] == [
            "Tok",
            "en",
            "ized",
            " mock",
            " answer",
        ]
        assert len(token_chunks) == 5
        # Terminal sentinel.
        assert chunks[-1] == AgentChunk(kind="done", data="")
        # Exactly one done chunk.
        assert sum(1 for c in chunks if c.kind == "done") == 1

    @pytest.mark.asyncio
    async def test_tokens_spaced_by_at_least_50ms(self) -> None:
        """The mock simulates network latency — consecutive tokens ≥0.05s apart."""
        mock = _MockLangChainAgentAdapter()

        timestamps: list[float] = []
        async for chunk in mock.stream(AgentRequest(question="hi"), []):
            if chunk.kind == "token":
                timestamps.append(time.monotonic())

        # 5 timestamps → 4 gaps; each must be ≥ 0.05s.
        gaps = [b - a for a, b in itertools.pairwise(timestamps)]
        assert len(gaps) == 4
        for gap in gaps:
            assert gap >= 0.05, f"token gap {gap}s is below the 0.05s floor"

    @pytest.mark.asyncio
    async def test_total_elapsed_at_least_250ms(self) -> None:
        """5 tokens (>=0.05s spacing) -> >=0.25s total."""
        mock = _MockLangChainAgentAdapter()

        start = time.monotonic()
        async for _ in mock.stream(AgentRequest(question="hi"), []):
            pass
        elapsed = time.monotonic() - start

        assert elapsed >= 0.25

    @pytest.mark.asyncio
    async def test_run_still_works_unmodified(self) -> None:
        """Mock ``run`` MUST remain unchanged — MCP tool regression guard."""
        mock = _MockLangChainAgentAdapter()

        response = await mock.run(AgentRequest(question="hi"), [])

        assert response.answer == "[mock answer to: hi]"
