"""Embedding port — application-layer contract for batch embedding adapters.

Use cases depend on this Protocol, never on a concrete embedding provider
(``GeminiEmbeddingAdapter``, ``MockEmbeddingAdapter``). The Protocol is
structural: any class with the right method signature satisfies it
without inheritance. This is the single-responsibility split from SOLID
that keeps the ``PreindexUseCase`` testable with an in-memory fake.

Why a single ``embed(texts: list[str]) -> list[list[float]]`` method and
NOT the design.md ``embed_one``/``embed_many`` pair? Per the orchestrator's
PR2 spec, batch-only is the contract — the preindex pipeline always sends
chunks in batches, never one-by-one. Single-method keeps the port minimal
(Interface Segregation Principle).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingPort(Protocol):
    """Contract for any embedding adapter.

    Implementations MUST return one vector per input text, where each
    vector's length equals the configured ``embedding_dim`` (default 768).
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts.

        Args:
            texts: Non-empty list of input strings to embed.

        Returns:
            List of vectors (one per input). Each vector's length matches
            ``embedding_dim``. Order is preserved: ``result[i]`` corresponds
            to ``texts[i]``.
        """
        ...


__all__ = ["EmbeddingPort"]
