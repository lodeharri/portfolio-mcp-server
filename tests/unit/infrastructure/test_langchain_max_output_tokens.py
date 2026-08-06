"""Regression tests for the LLM prompt discipline caps.

The 003-playground-ui llm-prompt-discipline spec (PR2a subset) caps
``ask_portfolio`` at ``max_output_tokens=600`` (Decision #12 — short-first
invariant; the spec author reduced the agent's per-call budget by ~30 %
from the open-ended default so the recruiter-demo response stays
focussed).

The spec's original field name was ``UsageLimits.response_tokens_limit``
(Pydantic AI 2.x), but the project migrated to LangChain/LangGraph in
005-langchain-integration so the equivalent surface in PR2a is the
``max_output_tokens`` field on ``ChatGoogleGenerativeAI``. REL-8
finding from PR1's reliability review called this out — the apply
phase MUST use the LangChain-native field, not the Pydantic AI field.

Tests assert the field is set on the LLM instance used by
``LangChainAgentAdapter`` (the field is read at construction time and
propagated to every Gemini call). When a pre-built ``llm`` is
injected (test double), the adapter does NOT override it — that path
is owned by the test that built the double.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_google_genai import ChatGoogleGenerativeAI

from mcp_server.infrastructure.langchain import LangChainAgentAdapter


class _CaptureGoogleGenerativeAI:
    """Capture the kwargs passed to ``ChatGoogleGenerativeAI`` so we can assert them."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> ChatGoogleGenerativeAI:
        self.calls.append(kwargs)
        # Return a real ChatGoogleGenerativeAI so downstream code
        # doesn't choke on a mock that lacks Pydantic validation.
        # We persist the kwargs on a separate field for the test.
        return ChatGoogleGenerativeAI(**kwargs)


def test_langchain_adapter_passes_max_output_tokens_600(monkeypatch: pytest.MonkeyPatch) -> None:
    """``LangChainAgentAdapter(api_key=...)`` MUST build its LLM with ``max_output_tokens=600``."""
    capture = _CaptureGoogleGenerativeAI()
    monkeypatch.setattr(
        "mcp_server.infrastructure.langchain.ChatGoogleGenerativeAI",
        capture,
    )

    LangChainAgentAdapter(api_key="dummy-key")

    assert len(capture.calls) == 1
    kwargs = capture.calls[0]
    assert kwargs["max_output_tokens"] == 600
    # Sanity: the model + api_key are also passed (no regression).
    assert kwargs["model"] == "gemini-flash-latest"
    assert kwargs["api_key"] == "dummy-key"


def test_langchain_adapter_uses_spec_model_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default model MUST match ``gemini-flash-latest`` (composition.py AGENT_MODEL_NAME)."""
    capture = _CaptureGoogleGenerativeAI()
    monkeypatch.setattr(
        "mcp_server.infrastructure.langchain.ChatGoogleGenerativeAI",
        capture,
    )

    LangChainAgentAdapter(api_key="dummy")

    assert capture.calls[0]["model"] == "gemini-flash-latest"


def test_langchain_adapter_preserves_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit model argument MUST override the default."""
    capture = _CaptureGoogleGenerativeAI()
    monkeypatch.setattr(
        "mcp_server.infrastructure.langchain.ChatGoogleGenerativeAI",
        capture,
    )

    LangChainAgentAdapter(api_key="dummy", model="gemini-1.5-pro")

    assert capture.calls[0]["model"] == "gemini-1.5-pro"


def test_max_output_tokens_is_an_int_not_string() -> None:
    """``max_output_tokens`` MUST be an integer — Gemini rejects strings."""
    # Direct field check on the real constructor.
    import langchain_google_genai.chat_models as cm

    field = cm.ChatGoogleGenerativeAI.model_fields["max_output_tokens"]
    # Pydantic field annotation may be ``Optional[int]`` or similar.
    annotation = str(field.annotation)
    assert "int" in annotation, (
        f"max_output_tokens annotation must be int-typed, got: {annotation!r}"
    )


def test_factory_picks_real_adapter_for_non_empty_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_langchain_agent`` MUST return the real adapter when api_key is non-empty.

    Belt-and-suspenders: composition wires the real adapter through
    ``AGENT_MODEL_NAME``; if a future change accidentally flips the
    factory to mock mode under a populated key, the 600-cap field
    silently disappears and the ask_portfolio responses balloon.
    """
    monkeypatch.setattr(
        "mcp_server.infrastructure.langchain.ChatGoogleGenerativeAI",
        _CaptureGoogleGenerativeAI(),
    )

    from mcp_server.infrastructure.langchain import (
        _MockLangChainAgentAdapter,
        create_langchain_agent,
    )

    adapter = create_langchain_agent(api_key="dummy", model="gemini-flash-latest")

    assert not isinstance(adapter, _MockLangChainAgentAdapter), (
        "create_langchain_agent must NOT return the mock adapter when api_key is non-empty"
    )
    # Real adapter's underlying LLM has the 600 cap.
    assert getattr(adapter._llm, "max_output_tokens", None) == 600
