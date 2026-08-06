"""Unit tests for ``AskPortfolioUseCase.astream`` — the streaming variant.

Per the 003-playground-ui agent-streaming spec, ``astream`` MUST:

* Call ``self.rate_limiter.check(request.client_ip)`` exactly once
  before iterating the agent (same gate as ``aexecute``).
* Reject rate-limited requests without invoking the agent.
* Reject empty questions before iterating (parity with ``aexecute``).
* Call ``self.sanitizer.sanitize(token, source="ask_portfolio")`` on
  EVERY yielded token BEFORE yielding the chunk to the caller.
  Per-chunk sanitization is the Layer 3 invariant under SSE — the
  middleware can no longer catch the bytes because it buffers full
  bodies (ADR-005).
* Emit ``audit.warn("agent.tool_call", ...)`` once per tool_call
  chunk, mirroring ``aexecute``.
* Terminate with ``AskPortfolioChunk(kind="done", result=...)``
  whose ``result.answer`` is the concatenation of sanitized tokens.
* NOT yield a partial ``AskPortfolioResult`` if the agent raises
  mid-stream; instead yield an ``AskPortfolioChunk(kind="error")``
  carrying the stringified exception (REL-3) and terminate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from mcp_server.application.ports.agent import (
    AgentChunk,
    AgentRequest,
    AgentResponse,
)
from mcp_server.application.use_cases.ask_portfolio import (
    AskPortfolioRequest,
    AskPortfolioResult,
    AskPortfolioUseCase,
)
from mcp_server.domain.exceptions import RateLimitExceeded
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeStreamingAgent:
    """An ``AgentPort`` test double that yields a scripted AgentChunk stream.

    Defaults to the spec's mock-agent deterministic stream (5 tokens +
    done) so the tests that don't care about the agent's content still
    see the canonical shape. Tests that need tool calls or errors
    override ``chunks``.
    """

    def __init__(
        self,
        chunks: list[AgentChunk] | None = None,
        response: AgentResponse | None = None,
    ) -> None:
        self._chunks = (
            chunks
            if chunks is not None
            else [
                AgentChunk(kind="token", data="Tok"),
                AgentChunk(kind="token", data="en"),
                AgentChunk(kind="token", data="ized"),
                AgentChunk(kind="token", data=" mock"),
                AgentChunk(kind="token", data=" answer"),
                AgentChunk(kind="done", data=""),
            ]
        )
        self.response = response or AgentResponse(answer="clean answer")
        self.run_calls: list[tuple[AgentRequest, list[Any]]] = []
        self.stream_calls: list[tuple[AgentRequest, list[Any]]] = []

    async def run(self, request: AgentRequest, tools: list[Any]) -> AgentResponse:
        self.run_calls.append((request, tools))
        return self.response

    async def stream(self, request: AgentRequest, tools: list[Any]) -> AsyncIterator[AgentChunk]:
        self.stream_calls.append((request, tools))
        for chunk in self._chunks:
            yield chunk


class _FakeRateLimiter:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.calls: list[str] = []

    def check(self, client_ip: str) -> bool:
        self.calls.append(client_ip)
        return self.allow


class _CapturingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append(("info", event, fields))

    def warn(self, event: str, **fields: Any) -> None:
        self.events.append(("warn", event, fields))


def _make_use_case(
    agent: _FakeStreamingAgent | None = None,
    rate_limiter: _FakeRateLimiter | None = None,
) -> tuple[AskPortfolioUseCase, _FakeStreamingAgent, _CapturingAudit, _FakeRateLimiter]:
    agent = agent or _FakeStreamingAgent()
    audit = _CapturingAudit()
    sanitizer = OutputSanitizer(audit=AuditLogger())  # silent — no events leak
    rate_limiter = rate_limiter or _FakeRateLimiter()
    use_case = AskPortfolioUseCase(
        agent=agent,
        tools=["tool"],
        sanitizer=sanitizer,
        audit=audit,  # type: ignore[arg-type]
        rate_limiter=rate_limiter,
    )
    return use_case, agent, audit, rate_limiter


# ---------------------------------------------------------------------------
# Rate-limit gate (Layer 5 invariant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limit_check_fires_once_before_stream() -> None:
    """``rate_limiter.check`` MUST be called exactly once per request."""
    use_case, _, _, limiter = _make_use_case()

    chunks: list[Any] = []
    async for chunk in use_case.astream(AskPortfolioRequest(question="hi")):
        chunks.append(chunk)

    assert limiter.calls == ["127.0.0.1"]
    # Terminal sentinel MUST be present.
    assert any(c.kind == "done" for c in chunks)


@pytest.mark.asyncio
async def test_rate_limit_blocked_skips_agent_stream() -> None:
    """When the limiter rejects, ``astream`` MUST raise without touching the agent."""
    use_case, agent, _, _ = _make_use_case(rate_limiter=_FakeRateLimiter(allow=False))

    with pytest.raises(RateLimitExceeded):
        async for _ in use_case.astream(AskPortfolioRequest(question="hi")):
            pass

    assert agent.stream_calls == []


@pytest.mark.asyncio
async def test_empty_question_rejected_before_stream() -> None:
    """Empty/whitespace questions MUST raise ``ValueError`` (parity with aexecute)."""
    use_case, agent, _, _ = _make_use_case()

    with pytest.raises(ValueError, match="non-empty"):
        async for _ in use_case.astream(AskPortfolioRequest(question="   ")):
            pass

    assert agent.stream_calls == []


# ---------------------------------------------------------------------------
# Per-token sanitization (Layer 3 invariant under SSE)
# ---------------------------------------------------------------------------


class _SpySanitizer:
    """A sanitizer test-double that records each ``sanitize`` call.

    Tracks the call count so tests can assert ``sanitize`` was called
    once per token — not once for the concatenated total. Returns the
    input verbatim unless a token contains ``AKIA`` (then returns
    ``'X redaction'``); this gives a deterministic redaction
    observation point for the test.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def sanitize(self, text: str, source: str):  # type: ignore[no-untyped-def]
        from mcp_server.security.output_sanitizer import SanitizedOutput

        self.calls.append((text, source))
        if "AKIA" in text:
            return SanitizedOutput(redacted_text="X redaction", incidents=[])
        return SanitizedOutput(redacted_text=text, incidents=[])


def _make_use_case_with_spy(
    spy: _SpySanitizer,
    agent: _FakeStreamingAgent | None = None,
) -> AskPortfolioUseCase:
    audit = _CapturingAudit()
    use_case = AskPortfolioUseCase(
        agent=agent or _FakeStreamingAgent(),
        tools=["tool"],
        sanitizer=spy,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        rate_limiter=_FakeRateLimiter(),
    )
    return use_case


@pytest.mark.asyncio
async def test_each_token_sanitized_with_correct_source() -> None:
    """``sanitize`` MUST be called once per token with source='ask_portfolio'."""
    spy = _SpySanitizer()
    use_case = _make_use_case_with_spy(spy)

    async for _ in use_case.astream(AskPortfolioRequest(question="hi")):
        pass

    # Mock agent yields 5 token chunks; each one MUST be sanitized.
    assert [text for text, _ in spy.calls] == [
        "Tok",
        "en",
        "ized",
        " mock",
        " answer",
    ]
    assert all(source == "ask_portfolio" for _, source in spy.calls)


@pytest.mark.asyncio
async def test_redaction_applied_per_token_not_to_concatenation() -> None:
    """A secret inside a token MUST be redacted in that chunk's output.

    Triangulation: a token that contains the literal string
    ``AKIAIOSFODNN7EXAMPLE`` MUST be replaced with the redacted form
    in the yielded ``answer_token`` — the use case cannot defer to a
    single post-stream sanitize because the middleware can no longer
    catch SSE bytes (ADR-005).
    """
    agent = _FakeStreamingAgent(
        chunks=[
            AgentChunk(kind="token", data="safe prefix "),
            AgentChunk(kind="token", data="AKIAIOSFODNN7EXAMPLE "),
            AgentChunk(kind="token", data="safe suffix"),
            AgentChunk(kind="done", data=""),
        ]
    )
    spy = _SpySanitizer()
    use_case = _make_use_case_with_spy(spy, agent=agent)

    tokens: list[str] = []
    async for chunk in use_case.astream(AskPortfolioRequest(question="hi")):
        if chunk.kind == "token":
            tokens.append(chunk.answer_token or "")

    assert tokens == ["safe prefix ", "X redaction", "safe suffix"]
    # Sanitize MUST have been called per-token (3 token chunks = 3 calls).
    assert len(spy.calls) == 3


@pytest.mark.asyncio
async def test_clean_tokens_pass_through_verbatim() -> None:
    """Clean tokens (no secret patterns) MUST pass through unchanged."""
    use_case, _, _, _ = _make_use_case()

    tokens: list[str] = []
    async for chunk in use_case.astream(AskPortfolioRequest(question="hi")):
        if chunk.kind == "token":
            tokens.append(chunk.answer_token or "")

    assert "".join(tokens) == "Tokenized mock answer"
    # All tokens verbatim — no redaction expected for clean input.


# ---------------------------------------------------------------------------
# Tool-call audit events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_emits_audit_event_per_invocation() -> None:
    """Each ``AgentChunk(kind='tool_call')`` MUST emit one ``agent.tool_call`` event."""
    agent = _FakeStreamingAgent(
        chunks=[
            AgentChunk(kind="token", data="Looking..."),
            AgentChunk(kind="tool_call", data={"name": "list_projects"}),
            AgentChunk(kind="token", data="..."),
            AgentChunk(kind="tool_call", data={"name": "search_code"}),
            AgentChunk(kind="done", data=""),
        ]
    )
    use_case, _, audit, _ = _make_use_case(agent=agent)

    async for _ in use_case.astream(AskPortfolioRequest(question="hi")):
        pass

    tool_events = [
        (level, event, fields)
        for level, event, fields in audit.events
        if event == "agent.tool_call"
    ]
    # Exactly two tool events, one per invocation.
    assert len(tool_events) == 2
    assert (
        "warn",
        "agent.tool_call",
        {"tool": "list_projects", "source": "ask_portfolio"},
    ) in tool_events
    assert (
        "warn",
        "agent.tool_call",
        {"tool": "search_code", "source": "ask_portfolio"},
    ) in tool_events


@pytest.mark.asyncio
async def test_no_audit_event_for_tokens() -> None:
    """Token chunks MUST NOT emit ``agent.tool_call`` events."""
    use_case, _, audit, _ = _make_use_case()

    async for _ in use_case.astream(AskPortfolioRequest(question="hi")):
        pass

    tool_events = [e for e in audit.events if e[1] == "agent.tool_call"]
    assert tool_events == []


# ---------------------------------------------------------------------------
# DONE chunk + final AskPortfolioResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_chunk_carries_sanitized_final_result() -> None:
    """The terminal ``done`` chunk MUST carry ``AskPortfolioResult`` with
    ``answer`` = concatenation of sanitized tokens + ``tools_called`` +
    ``conversation_id``.
    """
    agent = _FakeStreamingAgent(
        chunks=[
            AgentChunk(kind="token", data="Tok"),
            AgentChunk(kind="token", data="en"),
            AgentChunk(kind="tool_call", data={"name": "list_projects"}),
            AgentChunk(kind="token", data="ized mock answer"),
            AgentChunk(kind="done", data=""),
        ]
    )
    use_case, _, _, _ = _make_use_case(agent=agent)

    done_chunk = None
    async for chunk in use_case.astream(
        AskPortfolioRequest(question="hi", conversation_id="conv-42")
    ):
        if chunk.kind == "done":
            done_chunk = chunk

    assert done_chunk is not None
    assert done_chunk.kind == "done"
    assert isinstance(done_chunk.result, AskPortfolioResult)
    assert done_chunk.result.answer == "Tokenized mock answer"
    assert done_chunk.result.tools_called == ["list_projects"]
    assert done_chunk.result.conversation_id == "conv-42"


# ---------------------------------------------------------------------------
# Mid-stream exception → REL-3 error chunk
# ---------------------------------------------------------------------------


class _RaisingStreamingAgent:
    """An agent whose ``stream`` raises mid-stream after one token."""

    def __init__(self) -> None:
        self.stream_calls = 0

    async def run(self, request: AgentRequest, tools: list[Any]) -> AgentResponse:
        return AgentResponse(answer="clean")

    async def stream(self, request: AgentRequest, tools: list[Any]) -> AsyncIterator[AgentChunk]:
        self.stream_calls += 1
        yield AgentChunk(kind="token", data="partial ")
        raise RuntimeError("langgraph upstream timeout")


@pytest.mark.asyncio
async def test_mid_stream_exception_yields_error_chunk_not_result() -> None:
    """A mid-stream exception MUST become an ``error`` chunk + termination.

    Per REL-3: the SSE layer translates this to ``data: [ERROR]\\n\\n``.
    No partial ``AskPortfolioResult`` MUST be yielded.
    """
    use_case, _, _, _ = _make_use_case(agent=_RaisingStreamingAgent())  # type: ignore[arg-type]

    seen_kinds: list[str] = []
    done_seen = False
    error_seen = False
    async for chunk in use_case.astream(AskPortfolioRequest(question="hi")):
        seen_kinds.append(chunk.kind)
        if chunk.kind == "done":
            done_seen = True
        if chunk.kind == "error":
            error_seen = True
            assert chunk.error is not None
            assert "langgraph upstream timeout" in chunk.error

    assert error_seen, f"REL-3 violated — expected an error chunk, got kinds: {seen_kinds!r}"
    assert not done_seen, "no partial AskPortfolioResult MUST be yielded on mid-stream failure"


@pytest.mark.asyncio
async def test_error_chunk_does_not_emit_tool_completed_audit() -> None:
    """An error path MUST NOT emit ``tool.completed`` — the work never finished."""
    use_case, _, audit, _ = _make_use_case(agent=_RaisingStreamingAgent())  # type: ignore[arg-type]

    async for _ in use_case.astream(AskPortfolioRequest(question="hi")):
        pass

    completed = [e for e in audit.events if e[1] == "tool.completed"]
    assert completed == []


# ---------------------------------------------------------------------------
# Mock agent parity (sanity)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_yields_at_least_two_chunks_within_five_seconds() -> None:
    """Integration gate — mock adapter through the use case MUST yield ≥2 chunks in <5s."""
    use_case, _, _, _ = _make_use_case()

    import time

    chunks: list[Any] = []
    start = time.monotonic()
    async for chunk in use_case.astream(AskPortfolioRequest(question="hi")):
        chunks.append(chunk)
    elapsed = time.monotonic() - start

    # Mock yields 5 tokens + 1 done = 6 chunks. We require >=2.
    assert len(chunks) >= 2
    # Mock tokens are spaced 50ms apart (5 x 0.05s = 0.25s minimum).
    # We don't assert the floor here (that's the adapter's contract).
    # We DO assert the whole stream completes within 5s — the orchestrator gate.
    assert elapsed < 5.0
