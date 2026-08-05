"""Unit tests for ``src/mcp_server/infrastructure/adapters/gemini_llm.py``.

The LLM adapter implements :class:`LLMPort`:

* ``summarize(text, max_tokens=500) -> str`` — used by the future
  ``summarize_readme`` MCP tool (not exercised in PR3 but the
  contract is required by the composition wiring).
* ``chat(messages, tools=None) -> str`` — used by the future
  ``ask_portfolio`` agent (PR4 territory; PR3 wires the port only).

The adapter applies the same retry policy as the embedding adapter
(ADR-003): 3 attempts, full jitter, fail-fast on 4xx ≠ 429.

A :class:`MockLlmAdapter` (deterministic) is provided so future MCP
tools / use cases can be unit-tested without the SDK or rate-limited
free tier.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


class TestMockLlmAdapter:
    """``MockLlmAdapter`` is deterministic + no-network."""

    def test_summarize_returns_a_string(self) -> None:
        from mcp_server.infrastructure.adapters.gemini_llm import MockLlmAdapter

        adapter = MockLlmAdapter()
        result = adapter.summarize("hello world this is a long text")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_summarize_truncates_to_max_tokens(self) -> None:
        from mcp_server.infrastructure.adapters.gemini_llm import MockLlmAdapter

        adapter = MockLlmAdapter()
        long = "word " * 1000
        result = adapter.summarize(long, max_tokens=10)
        # 10 tokens ~= 10 words for the mock.
        assert result.count(" ") <= 12

    def test_chat_returns_string(self) -> None:
        from mcp_server.infrastructure.adapters.gemini_llm import MockLlmAdapter

        adapter = MockLlmAdapter()
        result = adapter.chat(
            [{"role": "user", "content": "hi"}],
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_chat_with_tools_returns_string(self) -> None:
        from mcp_server.infrastructure.adapters.gemini_llm import MockLlmAdapter

        adapter = MockLlmAdapter()
        result = adapter.chat(
            [{"role": "user", "content": "hi"}],
            tools=[{"name": "echo", "parameters": {}}],
        )
        assert isinstance(result, str)

    def test_mock_satisfies_llm_port(self) -> None:
        from mcp_server.application.ports.llm import LLMPort
        from mcp_server.infrastructure.adapters.gemini_llm import MockLlmAdapter

        adapter = MockLlmAdapter()
        assert isinstance(adapter, LLMPort)


# ---------------------------------------------------------------------------
# Real adapter — happy path (SDK stubbed)
# ---------------------------------------------------------------------------


def _build_fake_client(text_response: str) -> MagicMock:
    """Build a fake Gemini client returning the given text."""
    client = MagicMock()
    response = MagicMock()
    response.text = text_response
    response.candidates = [MagicMock()]
    response.candidates[0].content.parts = [MagicMock()]
    response.candidates[0].content.parts[0].text = text_response
    client.generate_content = MagicMock(return_value=response)
    return client


class TestGeminiLlmAdapterHappyPath:
    """The real adapter returns text from generate_content."""

    def test_summarize_returns_text(self, monkeypatch) -> None:
        from mcp_server.infrastructure.adapters import gemini_llm as gl

        fake = _build_fake_client("a tiny summary")
        monkeypatch.setattr(gl, "_build_genai_client", lambda api_key: fake)
        monkeypatch.setattr(gl.time, "sleep", lambda _s: None)

        adapter = gl.GeminiLlmAdapter(api_key="dummy")
        result = adapter.summarize("a very long text that needs summarizing")
        assert isinstance(result, str)
        assert "a tiny summary" in result

    def test_chat_returns_text(self, monkeypatch) -> None:
        from mcp_server.infrastructure.adapters import gemini_llm as gl

        fake = _build_fake_client("hello back")
        monkeypatch.setattr(gl, "_build_genai_client", lambda api_key: fake)
        monkeypatch.setattr(gl.time, "sleep", lambda _s: None)

        adapter = gl.GeminiLlmAdapter(api_key="dummy")
        result = adapter.chat([{"role": "user", "content": "hi"}])
        assert "hello back" in result

    def test_real_adapter_satisfies_llm_port(self, monkeypatch) -> None:
        from mcp_server.application.ports.llm import LLMPort
        from mcp_server.infrastructure.adapters import gemini_llm as gl

        fake = _build_fake_client("ok")
        monkeypatch.setattr(gl, "_build_genai_client", lambda api_key: fake)
        monkeypatch.setattr(gl.time, "sleep", lambda _s: None)

        adapter = gl.GeminiLlmAdapter(api_key="dummy")
        assert isinstance(adapter, LLMPort)


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


class TestRetryPolicy:
    """Same ADR-003 retry budget as the embedding adapter."""

    def test_429_three_times_raises_transient(self, monkeypatch) -> None:
        from mcp_server.domain.exceptions import GeminiTransientError
        from mcp_server.infrastructure.adapters import gemini_llm as gl

        def _fail(**_kwargs: Any) -> Any:
            exc = MagicMock()
            exc.status_code = 429
            raise type("RateLimit", (Exception,), {"status_code": 429})("rate")

        fake = MagicMock()
        fake.generate_content = MagicMock(side_effect=_fail)
        monkeypatch.setattr(gl, "_build_genai_client", lambda api_key: fake)
        monkeypatch.setattr(gl.time, "sleep", lambda _s: None)

        adapter = gl.GeminiLlmAdapter(api_key="dummy")
        with pytest.raises(GeminiTransientError):
            adapter.summarize("hi")

    def test_400_raises_permanent_no_sleep(self, monkeypatch) -> None:
        from mcp_server.domain.exceptions import GeminiPermanentError
        from mcp_server.infrastructure.adapters import gemini_llm as gl

        def _bad(**_kwargs: Any) -> Any:
            exc = MagicMock()
            exc.status_code = 400
            raise type("BadRequest", (Exception,), {"status_code": 400})("bad")

        fake = MagicMock()
        fake.generate_content = MagicMock(side_effect=_bad)
        monkeypatch.setattr(gl, "_build_genai_client", lambda api_key: fake)

        sleeps: list[float] = []
        monkeypatch.setattr(gl.time, "sleep", lambda s: sleeps.append(s))

        adapter = gl.GeminiLlmAdapter(api_key="dummy")
        with pytest.raises(GeminiPermanentError):
            adapter.summarize("hi")
        # 1 attempt + 0 sleeps = fail-fast.
        assert fake.generate_content.call_count == 1
        assert sleeps == []


# ---------------------------------------------------------------------------
# Retry budget constants — pinned to ADR-003
# ---------------------------------------------------------------------------


class TestRetryBudgetConstants:
    def test_max_attempts_is_3(self) -> None:
        from mcp_server.infrastructure.adapters import gemini_llm as gl

        assert gl.MAX_ATTEMPTS == 3
