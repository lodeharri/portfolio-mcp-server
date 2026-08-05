"""Conformance tests for ``src/mcp_server/application/ports/llm.py``.

The :class:`LLMPort` Protocol declares the contract an LLM adapter must
satisfy. Per the orchestrator's PR2 spec, it has two methods:

* ``summarize(text: str, max_tokens: int = 500) -> str``
* ``chat(messages: list[dict], tools: list[dict] | None = None) -> str``

Used by future MCP tools (`explain_architecture`, `summarize_readme`,
`ask_portfolio` agent) — not exercised in PR2.
"""

from __future__ import annotations

import inspect


class TestLLMPortProtocol:
    """``LLMPort`` declares the contract for LLM-backed text generation."""

    def test_llm_port_protocol_exists(self) -> None:
        from mcp_server.application.ports.llm import LLMPort

        assert LLMPort is not None

    def test_llm_port_has_summarize(self) -> None:
        from mcp_server.application.ports.llm import LLMPort

        members = dict(inspect.getmembers(LLMPort))
        assert "summarize" in members

    def test_llm_port_has_chat(self) -> None:
        from mcp_server.application.ports.llm import LLMPort

        members = dict(inspect.getmembers(LLMPort))
        assert "chat" in members


class TestLLMPortConformance:
    """A class with the right methods satisfies ``LLMPort``."""

    def test_fake_llm_satisfies_protocol(self) -> None:
        from mcp_server.application.ports.llm import LLMPort

        class FakeLLM:
            """Minimal stub satisfying LLMPort."""

            def summarize(self, text: str, max_tokens: int = 500) -> str:
                return f"summary({len(text)})"

            def chat(self, messages: list[dict], tools: list[dict] | None = None) -> str:
                return "fake-reply"

        fake = FakeLLM()
        assert isinstance(fake, LLMPort)

    def test_fake_llm_summarize_signature(self) -> None:
        """``summarize`` accepts ``max_tokens`` as a keyword argument."""

        class FakeLLM:
            def summarize(self, text: str, max_tokens: int = 500) -> str:
                return f"{text[:max_tokens]}"

            def chat(self, messages: list[dict], tools: list[dict] | None = None) -> str:
                return ""

        fake = FakeLLM()
        assert fake.summarize("hello world", max_tokens=3) == "hel"

    def test_class_without_methods_does_not_satisfy_protocol(self) -> None:
        from mcp_server.application.ports.llm import LLMPort

        class NotAnLLM:
            pass

        assert not isinstance(NotAnLLM(), LLMPort)
