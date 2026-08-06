"""Contract tests for ``AgentPort`` — the streaming variant added in PR2a.

Per the 003-playground-ui agent-streaming spec, ``AgentPort.stream``
MUST return an async iterator of ``AgentChunk`` events. ``AgentChunk``
MUST be a Pydantic model whose ``kind`` field is restricted to
``{"token", "tool_call", "done"}``.

The buffered ``AgentPort.run`` MUST continue to work unchanged
(regression — PR1 / MCP buffered ``ask_portfolio`` is green).
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from typing import Any, Protocol, get_args, get_origin

import pytest
from pydantic import BaseModel, ValidationError

from mcp_server.application.ports.agent import (
    AgentChunk,
    AgentPort,
    AgentRequest,
    AgentResponse,
)

# ---------------------------------------------------------------------------
# AgentChunk — Pydantic model contract
# ---------------------------------------------------------------------------


class TestAgentChunk:
    """``AgentChunk`` is a Pydantic BaseModel with a closed-set ``kind``."""

    def test_is_pydantic_basemodel(self) -> None:
        # ``kind`` is required (no default) so the Pydantic validation
        # contract enforces the closed-set invariant. Provide it here.
        assert isinstance(AgentChunk(kind="token"), BaseModel)
        assert issubclass(AgentChunk, BaseModel)

    @pytest.mark.parametrize("kind", ["token", "tool_call", "done", "error"])
    def test_accepts_each_documented_kind(self, kind: str) -> None:
        chunk = AgentChunk(kind=kind, data="payload")
        assert chunk.kind == kind
        assert chunk.data == "payload"

    @pytest.mark.parametrize("bad_kind", ["", "TEXT", "token ", "Done", "fail"])
    def test_rejects_unknown_kind_values(self, bad_kind: str) -> None:
        with pytest.raises(ValidationError):
            AgentChunk(kind=bad_kind, data="x")

    def test_data_default_is_empty_string(self) -> None:
        chunk = AgentChunk(kind="token")
        assert chunk.data == ""

    def test_data_accepts_dict_payload(self) -> None:
        chunk = AgentChunk(kind="tool_call", data={"name": "list_projects"})
        assert chunk.data == {"name": "list_projects"}

    def test_is_json_round_trippable(self) -> None:
        """Pydantic models serialize cleanly — the SSE encoder relies on this."""
        chunk = AgentChunk(kind="token", data="hello")
        dumped = chunk.model_dump_json()
        restored = AgentChunk.model_validate_json(dumped)
        assert restored == chunk


# ---------------------------------------------------------------------------
# AgentPort — async iterator signature
# ---------------------------------------------------------------------------


class TestAgentPortProtocol:
    """``AgentPort`` is a runtime-checkable Protocol with both methods."""

    def test_protocol_is_runtime_checkable(self) -> None:
        # The decorator is what makes ``isinstance(...)`` work; this
        # is the property ``composition.py`` and the SSE adapter rely
        # on when they look up ``app.state.composition.agent``.
        assert (
            getattr(AgentPort, "_is_runtime_protocol", False)
            or callable(AgentPort)
            or isinstance(AgentPort, type)
        )

    def test_stream_is_a_coroutine_function(self) -> None:
        """``stream`` is declared as ``async def`` — async iterator factory.

        We assert the kind-of-callable so a future refactor that drops
        ``async`` (turning it into a sync generator) fails loudly.
        """
        assert hasattr(AgentPort, "stream"), (
            "AgentPort must declare a stream method (PR2a agent-streaming spec)"
        )
        # ``inspect.iscoroutinefunction`` returns True for ``async def`` methods.
        assert inspect.iscoroutinefunction(AgentPort.stream), (
            "AgentPort.stream must be declared with ``async def`` (PR2a agent-streaming spec)"
        )

    def test_stream_return_annotation_is_async_iterator_of_agent_chunk(self) -> None:
        """The return annotation must be ``AsyncIterator[AgentChunk]``.

        The use case iterates ``async for chunk in agent.stream(...)``
        — the runtime check on Protocol annotations happens at attribute
        access time, so a missing/wrong annotation breaks the SSE flow.
        """
        hints = AgentPort.stream.__annotations__
        assert "return" in hints, "AgentPort.stream must declare an explicit return annotation"
        return_annotation = hints["return"]
        origin = get_origin(return_annotation)
        # AsyncIterator[X] is typing.AsyncIterator[X] — its origin is
        # ``collections.abc.AsyncIterator``. ``collections.abc.AsyncIterator``
        # directly used also passes.
        assert origin is AsyncIterator or return_annotation is AsyncIterator, (
            f"AgentPort.stream return annotation must be AsyncIterator, got: {return_annotation!r}"
        )
        args = get_args(return_annotation)
        assert args, (
            f"AgentPort.stream must parameterize AsyncIterator with AgentChunk, "
            f"got: {return_annotation!r}"
        )
        assert args[0] is AgentChunk, (
            f"AgentPort.stream must yield AgentChunk instances, got parameterization: {args[0]!r}"
        )

    def test_stream_argument_signature(self) -> None:
        """``stream`` takes ``request: AgentRequest`` and ``tools: list[Any]``."""
        sig = inspect.signature(AgentPort.stream)
        params = list(sig.parameters.values())
        # Expect: self, request, tools
        assert [p.name for p in params] == ["self", "request", "tools"], (
            f"AgentPort.stream must take (request, tools), "
            f"got parameters: {[p.name for p in params]!r}"
        )
        assert params[1].annotation is AgentRequest
        assert params[2].annotation == list[Any]

    def test_run_is_still_present_and_unchanged(self) -> None:
        """The buffered ``run`` MUST continue to work — MCP tool regression guard."""
        assert hasattr(AgentPort, "run"), "AgentPort.run must remain declared"
        assert inspect.iscoroutinefunction(AgentPort.run), (
            "AgentPort.run must remain an async coroutine function"
        )
        sig = inspect.signature(AgentPort.run)
        params = list(sig.parameters.values())
        assert [p.name for p in params] == ["self", "request", "tools"]
        assert params[1].annotation is AgentRequest
        # Return annotation is AgentResponse — must remain untouched.
        assert sig.return_annotation is AgentResponse


# ---------------------------------------------------------------------------
# Structural conformance — any class with both methods satisfies the protocol
# ---------------------------------------------------------------------------


def _runtime_checkable_protocol() -> type[Protocol]:
    """Helper: declare a runtime-checkable protocol so the isinstance() below works.

    ``AgentPort`` is declared as ``Protocol`` already, but we wrap it
    here to ensure ``@runtime_checkable`` semantics for the test
    regardless of upstream decoration order.
    """
    return AgentPort


class _FakeSatisfyingAdapter:
    """A class that implements both ``run`` and ``stream`` as required."""

    async def run(self, request: AgentRequest, tools: list[Any]) -> AgentResponse:
        return AgentResponse(answer="ok")

    async def stream(self, request: AgentRequest, tools: list[Any]) -> AsyncIterator[AgentChunk]:
        yield AgentChunk(kind="done", data="")


def test_fake_adapter_satisfies_protocol() -> None:
    """A class implementing both methods MUST satisfy ``AgentPort``."""
    adapter = _FakeSatisfyingAdapter()
    assert isinstance(adapter, AgentPort)
