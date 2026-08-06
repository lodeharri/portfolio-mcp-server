"""LLM port — application-layer contract for LLM-backed text generation.

Used by future MCP tools (``explain_architecture``, ``summarize_readme``,
``ask_portfolio`` agent). PR2 declares the contract; the concrete
``GeminiLLMAdapter`` lands in change 002-mcp-tools.

Two methods because the tools have distinct shapes:

* ``summarize`` — single-shot text compression (used by
  ``summarize_readme``).
* ``chat`` — multi-turn conversation with optional tool-calling (used by
  the ``ask_portfolio`` LangChain agent in the playground).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMPort(Protocol):
    """Contract for any LLM-backed adapter."""

    def summarize(self, text: str, max_tokens: int = 500) -> str:
        """Compress ``text`` into a summary of at most ``max_tokens`` tokens.

        Args:
            text: Source text (typically a README or architecture doc).
            max_tokens: Soft upper bound on output length. Implementations
                SHOULD honour it; exceeding by a small margin is acceptable.

        Returns:
            A summary string. Implementations MUST return a non-empty
            string for non-empty input.
        """
        ...

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> str:
        """Multi-turn chat completion with optional tool definitions.

        Args:
            messages: OpenAI-style message list ``[{"role": ..., "content": ...}, ...]``.
            tools: Optional list of tool definitions in OpenAI function-calling
                schema. When ``None``, the model is invoked without tool access.

        Returns:
            The assistant's reply text. Tool-call responses (when tools are
            bound) are resolved by the caller; this method returns the
            textual reply only.
        """
        ...


__all__ = ["LLMPort"]
