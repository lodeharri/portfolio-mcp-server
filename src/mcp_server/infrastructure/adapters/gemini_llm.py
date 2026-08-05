"""Gemini LLM adapter — implements :class:`LLMPort`.

Two operations:

* :meth:`summarize` — single-shot text compression (used by the
  future ``summarize_readme`` MCP tool — ``002-mcp-tools``).
* :meth:`chat` — multi-turn conversation with optional tool
  definitions (used by the future ``ask_portfolio`` Pydantic AI agent
  in the playground — PR4 territory).

The adapter re-uses the ADR-003 retry policy from
:meth:`mcp_server.infrastructure.adapters.gemini_embedding` — same
constants, same hand-rolled loop.

A :class:`MockLlmAdapter` is provided for tests so future MCP-tool use
cases don't have to mock the SDK.

SDK migration note
------------------

This module uses the new ``google-genai`` SDK (the official replacement
for the deprecated ``google-generativeai``). See the embedding
adapter's docstring for the size rationale.
"""

from __future__ import annotations

import random
import time
from typing import Final, Protocol

from google import genai
from google.genai import types

from mcp_server.application.ports.llm import LLMPort
from mcp_server.domain.exceptions import GeminiPermanentError, GeminiTransientError

__all__ = [
    "MAX_ATTEMPTS",
    "BASE_DELAY",
    "MAX_DELAY",
    "GeminiLlmAdapter",
    "MockLlmAdapter",
]

# ---------------------------------------------------------------------------
# ADR-003 retry budget — mirrored from the embedding adapter for clarity
# ---------------------------------------------------------------------------

MAX_ATTEMPTS: Final[int] = 3
BASE_DELAY: Final[float] = 1.0
MAX_DELAY: Final[float] = 30.0
DEFAULT_MODEL: Final[str] = "gemini-2.0-flash"


# ---------------------------------------------------------------------------
# Pluggable client builder — tests monkeypatch this
# ---------------------------------------------------------------------------


def _build_genai_client(api_key: str) -> "genai.Client":
    """Build a real ``google.genai.Client`` for production use.

    The new SDK uses a stateless client created once with the API key;
    requests are bound to the model at call time. Tests override this
    function via the ``client_factory`` constructor argument.
    """
    return genai.Client(api_key=api_key)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_from_exception(exc: BaseException) -> int | None:
    """Read a ``status_code`` attribute off the SDK exception if present."""
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return code
    return None


# ---------------------------------------------------------------------------
# Client protocol
# ---------------------------------------------------------------------------


class _LlmClientLike(Protocol):
    """Structural shape the LLM adapter needs from the SDK client."""

    def models(self) -> object: ...


# ---------------------------------------------------------------------------
# GeminiLlmAdapter — real adapter
# ---------------------------------------------------------------------------


class GeminiLlmAdapter:
    """Real :class:`LLMPort` impl backed by ``google-genai``.

    Args:
        api_key: Gemini API key.
        model: Gemini chat model. Default ``"gemini-2.0-flash"``.
        client_factory: Pluggable client builder; tests override this
            to inject a fake transport.
        clock: Pluggable sleep source.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        client_factory=None,
        clock=None,
    ) -> None:
        if not api_key:
            raise ValueError("GeminiLlmAdapter requires a non-empty api_key")
        self._model = model
        self._client: _LlmClientLike = (client_factory or _build_genai_client)(api_key)
        self._sleep = clock if clock is not None else time.sleep

    def summarize(self, text: str, max_tokens: int = 500) -> str:
        """Compress ``text`` via a single-shot prompt with retry policy."""
        prompt = (
            f"Summarize the following text in at most {max_tokens} tokens:\n\n"
            f"{text}"
        )
        return self._generate_with_retry(contents=prompt)

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> str:
        """Multi-turn chat completion with optional tool definitions.

        ``messages`` is the OpenAI-style list of
        ``{"role": ..., "content": ...}`` dicts translated to the new
        SDK's ``Content`` objects.
        """
        contents = [
            types.Content(
                role=m["role"],
                parts=[types.Part(text=m["content"])],
            )
            for m in messages
        ]
        return self._generate_with_retry(
            contents=contents,
            tools=tools,
        )

    def _generate_with_retry(
        self,
        *,
        contents,  # str OR list[types.Content]
        tools: list[dict] | None = None,
    ) -> str:
        """Run ``client.models.generate_content`` with the ADR-003 retry policy."""
        last_exc: BaseException | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                config = None
                if tools is not None:
                    config = types.GenerateContentConfig(tools=tools)
                response = self._client.models.generate_content(
                    model=self._model,
                    contents=contents,
                    config=config,
                )
                return self._extract_text(response)
            except GeminiPermanentError:
                raise
            except Exception as exc:  # noqa: BLE001
                status = _status_from_exception(exc)
                if status is not None and 400 <= status < 500 and status != 429:
                    raise GeminiPermanentError(
                        f"gemini chat failed with status {status}: {exc}"
                    ) from exc
                last_exc = exc
                if attempt < MAX_ATTEMPTS:
                    self._sleep_retry(attempt)
        raise GeminiTransientError(
            f"gemini chat exhausted {MAX_ATTEMPTS} retries: {last_exc}"
        ) from last_exc

    def _sleep_retry(self, attempt: int) -> None:
        computed = min(MAX_DELAY, BASE_DELAY * (2 ** (attempt - 1)))
        delay = random.uniform(0, computed)
        self._sleep(delay)

    @staticmethod
    def _extract_text(response: object) -> str:
        """Pull text content out of a google-genai ``GenerateContentResponse``."""
        # Prefer the convenience ``response.text`` attribute.
        text = getattr(response, "text", None)
        if isinstance(text, str) and text:
            return text
        # Fall back to walking the candidates list.
        candidates = getattr(response, "candidates", None)
        if isinstance(candidates, list) and candidates:
            first = candidates[0]
            content = getattr(first, "content", None)
            parts = getattr(content, "parts", None) if content else None
            if isinstance(parts, list) and parts:
                piece = parts[0]
                piece_text = getattr(piece, "text", None)
                if isinstance(piece_text, str):
                    return piece_text
        raise GeminiTransientError("malformed gemini chat response")


# ---------------------------------------------------------------------------
# MockLlmAdapter — deterministic, no-network
# ---------------------------------------------------------------------------


class MockLlmAdapter:
    """Deterministic :class:`LLMPort` impl. Same shape as the real one.

    * :meth:`summarize` returns the first ``max_tokens`` words of
      ``text`` (single split, no real NLP).
    * :meth:`chat` echoes back the last user message prefixed with
      ``"[mock] "``; ignores tools.
    """

    def summarize(self, text: str, max_tokens: int = 500) -> str:
        words = text.split()
        if not words:
            return ""
        # ``max_tokens`` ≈ words for the mock — exact 1:1 keeps the
        # contract simple. Tests assert on this ratio.
        return " ".join(words[:max_tokens])

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> str:
        if not messages:
            return "[mock] (no messages)"
        last = messages[-1]
        content = last.get("content", "") if isinstance(last, dict) else ""
        return f"[mock] {content}"
