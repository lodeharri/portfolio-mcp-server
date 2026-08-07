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
from types import SimpleNamespace
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

    def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _StubLangGraphAgent:
        captured["llm"] = llm
        captured["tools"] = list(tools)
        captured["kwargs"] = kwargs
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

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _CaptureAgent:
            captured["kwargs"] = kwargs
            return _CaptureAgent()

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())
        async for _ in adapter.stream(AgentRequest(question="hi", max_tool_calls=4), []):
            pass

        assert captured["stream_mode"] == "messages"
        assert captured["config"] == {"recursion_limit": 13}  # 4 * 3 + 1
        # The default portfolio prompt MUST be threaded through.
        prompt = captured["kwargs"].get("prompt")
        assert prompt
        assert "presupuesto" in prompt.lower() or "tool" in prompt.lower()

    @pytest.mark.asyncio
    async def test_yields_one_token_chunk_per_ai_message(
        self, stub_ai_messages, monkeypatch
    ) -> None:
        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())

        recorded: dict[str, Any] = {}

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _StubLangGraphAgent:
            recorded["tools"] = list(tools)
            recorded["kwargs"] = kwargs
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

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _StubLangGraphAgent:
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

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _StubLangGraphAgent:
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
    async def test_recursion_limit_is_max_tool_calls_times_3_plus_1(self, monkeypatch) -> None:
        """Recursion limit MUST equal ``request.max_tool_calls * 3 + 1``.

        ``* 3 + 1`` (one extra "answer attempt" step beyond the 6 of
        strict parity) is the policy that stops the "Recursion limit
        of 11" loop: the budget must allow one more turn than the
        tool calls + their responses to give the LLM room to commit
        to a final answer. Same formula as ``run``. Triangulation:
        vary ``max_tool_calls`` to confirm the formula scales linearly.
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

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _CaptureAgent:
            return _CaptureAgent()

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())
        for max_calls in (1, 5, 8):
            async for _ in adapter.stream(
                AgentRequest(question="hi", max_tool_calls=max_calls), []
            ):
                pass

        assert captured == [4, 16, 25]  # 1*3+1, 5*3+1, 8*3+1

    @pytest.mark.asyncio
    async def test_state_modifier_passed_to_create_react_agent_for_run(self, monkeypatch) -> None:
        """``LangChainAgentAdapter`` MUST thread its ``state_modifier`` through to
        ``create_react_agent`` so the ReAct agent gets explicit budget /
        language / portfolio-context instructions.

        LangGraph's ``create_react_agent(..., prompt=...)`` is the
        current API for setting the system prompt (the historical
        ``state_modifier`` kwarg was renamed). The adapter exposes the
        parameter as ``state_modifier`` for API clarity and passes it
        through to ``create_react_agent`` as ``prompt``.
        """
        captured: dict[str, Any] = {}

        class _CaptureAgent:
            async def ainvoke(
                self, payload: dict[str, Any], config: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                return {"messages": [SimpleNamespace(content="ok", tool_calls=[])]}

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _CaptureAgent:
            captured["kwargs"] = kwargs
            return _CaptureAgent()

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        custom_prompt = "You are a portfolio assistant. Respond in Spanish."
        adapter = LangChainAgentAdapter(api_key="dummy", llm=object(), state_modifier=custom_prompt)

        await adapter.run(AgentRequest(question="hi"), [])

        assert captured["kwargs"].get("prompt") == custom_prompt

    @pytest.mark.asyncio
    async def test_state_modifier_passed_to_create_react_agent_for_stream(
        self, monkeypatch
    ) -> None:
        """Same threading contract MUST hold for the ``stream`` path."""
        captured: dict[str, Any] = {}

        class _CaptureAgent:
            async def astream(
                self,
                payload: dict[str, Any],
                config: dict[str, Any] | None = None,
                *,
                stream_mode: str = "values",
            ) -> AsyncIterator[tuple[Any, dict[str, Any]]]:
                return
                yield  # pragma: no cover

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _CaptureAgent:
            captured["kwargs"] = kwargs
            return _CaptureAgent()

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        custom_prompt = "Stream-time portfolio prompt."
        adapter = LangChainAgentAdapter(api_key="dummy", llm=object(), state_modifier=custom_prompt)

        async for _ in adapter.stream(AgentRequest(question="hi"), []):
            pass

        assert captured["kwargs"].get("prompt") == custom_prompt

    @pytest.mark.asyncio
    async def test_state_modifier_consistent_between_run_and_stream(self, monkeypatch) -> None:
        """The SAME ``state_modifier`` MUST be passed to both ``run`` and
        ``stream`` — divergent prompts would give inconsistent UX
        between the two paths.
        """
        captured: list[dict[str, Any]] = []

        class _CaptureAgent:
            async def ainvoke(
                self, payload: dict[str, Any], config: dict[str, Any] | None = None
            ) -> dict[str, Any]:
                return {"messages": [SimpleNamespace(content="ok", tool_calls=[])]}

            async def astream(
                self,
                payload: dict[str, Any],
                config: dict[str, Any] | None = None,
                *,
                stream_mode: str = "values",
            ) -> AsyncIterator[tuple[Any, dict[str, Any]]]:
                return
                yield  # pragma: no cover

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _CaptureAgent:
            captured.append(kwargs)
            return _CaptureAgent()

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        shared_prompt = "Budget: 2 tool calls. Respond in Spanish."
        adapter = LangChainAgentAdapter(api_key="dummy", llm=object(), state_modifier=shared_prompt)

        await adapter.run(AgentRequest(question="hi"), [])
        async for _ in adapter.stream(AgentRequest(question="hi"), []):
            pass

        assert len(captured) == 2
        assert captured[0].get("prompt") == shared_prompt
        assert captured[1].get("prompt") == shared_prompt
        assert captured[0].get("prompt") == captured[1].get("prompt")

    @pytest.mark.asyncio
    async def test_default_state_modifier_is_portfolio_prompt(self, monkeypatch) -> None:
        """When the caller omits ``state_modifier``, the adapter MUST use the
        module-level portfolio prompt (not an empty string, not the
        LangGraph default English template).
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
                return
                yield  # pragma: no cover

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _CaptureAgent:
            captured["kwargs"] = kwargs
            return _CaptureAgent()

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())

        async for _ in adapter.stream(AgentRequest(question="hi"), []):
            pass

        prompt = captured["kwargs"].get("prompt")
        assert isinstance(prompt, str) and prompt.strip(), (
            "default state_modifier must be a non-empty string"
        )
        # The portfolio prompt MUST mention the user-language-matching rule
        # and the tool-call budget — the two instructions that fix the
        # "Recursion limit of 11" loop.
        assert "idioma" in prompt, "default prompt must mention language matching"
        assert "herramientas" in prompt or "tool" in prompt.lower(), (
            "default prompt must mention the tool-call budget"
        )

    @pytest.mark.asyncio
    async def test_done_chunk_terminates_stream(self, monkeypatch) -> None:
        """A terminal ``AgentChunk(kind='done', data='')`` MUST be yielded last."""

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _StubLangGraphAgent:
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

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _StubLangGraphAgent:
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

    @pytest.mark.asyncio
    async def test_skips_empty_content_shapes_and_normalizes_text_blocks(self, monkeypatch) -> None:
        class _FakeChatGoogleGenerativeAI:
            def __init__(self, **_kwargs: Any) -> None:
                pass

            async def astream(
                self,
                payload: dict[str, Any],
                config: dict[str, Any] | None = None,
                *,
                stream_mode: str = "values",
            ) -> AsyncIterator[tuple[Any, dict[str, Any]]]:
                assert stream_mode == "messages"
                for index, content in enumerate([[], "", None, "hello", ["partial text"]]):
                    if content is None:
                        message = AIMessageChunk.model_construct(content=None, id=str(index))
                    else:
                        message = AIMessageChunk(content=content)
                    yield message, {}

        monkeypatch.setattr(
            "mcp_server.infrastructure.langchain.ChatGoogleGenerativeAI",
            _FakeChatGoogleGenerativeAI,
        )
        monkeypatch.setattr(
            "mcp_server.infrastructure.langchain.create_react_agent",
            lambda llm, tools, **kwargs: llm,
        )

        adapter = LangChainAgentAdapter(api_key="dummy")

        chunks: list[AgentChunk] = []
        async for chunk in adapter.stream(AgentRequest(question="hi"), []):
            chunks.append(chunk)

        token_chunks = [chunk for chunk in chunks if chunk.kind == "token"]
        assert [chunk.data for chunk in token_chunks] == ["hello", "partial text"]
        assert chunks[-1] == AgentChunk(kind="done", data="")

    @pytest.mark.asyncio
    async def test_yields_tool_call_chunk_when_message_has_tool_calls(self, monkeypatch) -> None:
        """An ``AIMessageChunk`` carrying ``tool_calls`` MUST yield one
        ``AgentChunk(kind="tool_call", data=<dict>)`` per tool call,
        carrying ``name``, ``args``, and ``id`` for the frontend trace row.

        LangGraph emits the model's tool-call signal as a separate
        ``AIMessageChunk`` event whose ``content`` is ``""`` and
        ``tool_calls`` is a non-empty list. Without this translation
        the chat surface never sees tool invocations — the
        ask_portfolio audit pipeline stays silent and the recruiter
        demo cannot show "the agent actually used RAG".
        """
        tool_calls = [
            {
                "name": "search_code",
                "args": {"query": "rate limit"},
                "id": "toolu_abc",
                "type": "tool_call",
            }
        ]
        tool_chunk = AIMessageChunk.model_construct(content="", id="ai-1", tool_calls=tool_calls)

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _StubLangGraphAgent:
            return _StubLangGraphAgent([(tool_chunk, {"langgraph_node": "agent"})])

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())
        chunks: list[AgentChunk] = []
        async for chunk in adapter.stream(AgentRequest(question="hi"), []):
            chunks.append(chunk)

        tool_call_chunks = [c for c in chunks if c.kind == "tool_call"]
        assert len(tool_call_chunks) == 1, (
            f"expected 1 tool_call chunk; got {len(tool_call_chunks)}; "
            f"all chunks: {[(c.kind, c.data) for c in chunks]}"
        )
        payload = tool_call_chunks[0].data
        assert isinstance(payload, dict)
        assert payload["name"] == "search_code"
        assert payload["args"] == {"query": "rate limit"}
        assert payload["id"] == "toolu_abc"
        # No token chunks emitted for a content="" tool-call message.
        assert [c for c in chunks if c.kind == "token"] == []
        # Terminal sentinel still arrives.
        assert chunks[-1].kind == "done"

    @pytest.mark.asyncio
    async def test_yields_one_tool_call_chunk_per_tool_in_a_multi_tool_chunk(
        self, monkeypatch
    ) -> None:
        """When a single ``AIMessageChunk`` carries N tool calls the adapter
        MUST yield exactly N ``kind="tool_call"`` chunks — one per tool —
        in the order LangGraph emits them. Same-model multi-tool handoff
        (e.g. ``list_projects`` + ``search_code`` in the same step)
        must surface both pills to the UI.
        """
        tool_calls = [
            {
                "name": "list_projects",
                "args": {},
                "id": "toolu_1",
                "type": "tool_call",
            },
            {
                "name": "search_code",
                "args": {"query": "rate limit", "project_id": "finance-coach-latam"},
                "id": "toolu_2",
                "type": "tool_call",
            },
        ]
        tool_chunk = AIMessageChunk.model_construct(
            content="", id="ai-multi", tool_calls=tool_calls
        )

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _StubLangGraphAgent:
            return _StubLangGraphAgent([(tool_chunk, {"langgraph_node": "agent"})])

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())
        chunks: list[AgentChunk] = []
        async for chunk in adapter.stream(AgentRequest(question="hi"), []):
            chunks.append(chunk)

        tool_call_chunks = [c for c in chunks if c.kind == "tool_call"]
        assert len(tool_call_chunks) == 2
        assert [(c.data["name"], c.data["id"]) for c in tool_call_chunks] == [
            ("list_projects", "toolu_1"),
            ("search_code", "toolu_2"),
        ]
        # Second tool's args dict round-trips intact (the recruiter demo
        # wants to see "rate limit" + "finance-coach-latam" as the pill args).
        assert tool_call_chunks[1].data["args"] == {
            "query": "rate limit",
            "project_id": "finance-coach-latam",
        }

    @pytest.mark.asyncio
    async def test_tool_call_chunks_come_before_token_for_same_message(self, monkeypatch) -> None:
        """When a chunk carries BOTH ``tool_calls`` and non-empty ``content``
        (the model narrates "Let me search…" and then dispatches the tool)
        the adapter MUST yield the tool_call chunks FIRST, then the token.

        Mirrors the model's thinking order: intent to call the tool
        arrives as a structured signal before the prose token. The UI
        renders the trace pills above the body so the chronology has
        to match what the model emitted.
        """
        tool_calls = [
            {
                "name": "search_code",
                "args": {"query": "rate limit"},
                "id": "toolu_z",
                "type": "tool_call",
            }
        ]
        mixed_chunk = AIMessageChunk.model_construct(
            content="Let me look that up.", id="ai-mix", tool_calls=tool_calls
        )

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _StubLangGraphAgent:
            return _StubLangGraphAgent([(mixed_chunk, {"langgraph_node": "agent"})])

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())
        chunks: list[AgentChunk] = []
        async for chunk in adapter.stream(AgentRequest(question="hi"), []):
            chunks.append(chunk)

        kinds = [c.kind for c in chunks]
        # Exactly one tool_call then one token, then done.
        assert kinds == ["tool_call", "token", "done"]
        assert chunks[0].data["name"] == "search_code"
        assert chunks[1].data == "Let me look that up."

    @pytest.mark.asyncio
    async def test_does_not_yield_tool_call_chunk_when_tool_calls_is_empty(
        self, monkeypatch
    ) -> None:
        """A plain text-only ``AIMessageChunk`` (no tool calls) MUST yield
        exactly one ``kind="token"`` chunk and NO ``kind="tool_call"``
        chunk — preserving the pre-existing single-token path.
        """
        plain_chunk = AIMessageChunk.model_construct(content="hello", id="ai-text", tool_calls=[])

        def _factory(llm: Any, tools: list[Any], **kwargs: Any) -> _StubLangGraphAgent:
            return _StubLangGraphAgent([(plain_chunk, {"langgraph_node": "agent"})])

        monkeypatch.setattr("mcp_server.infrastructure.langchain.create_react_agent", _factory)

        adapter = LangChainAgentAdapter(api_key="dummy", llm=object())
        chunks: list[AgentChunk] = []
        async for chunk in adapter.stream(AgentRequest(question="hi"), []):
            chunks.append(chunk)

        token_chunks = [c for c in chunks if c.kind == "token"]
        tool_call_chunks = [c for c in chunks if c.kind == "tool_call"]
        assert [c.data for c in token_chunks] == ["hello"]
        assert tool_call_chunks == [], (
            f"text-only chunk must not produce tool_call chunks; got "
            f"{[c.data for c in tool_call_chunks]}"
        )
        assert chunks[-1].kind == "done"


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
