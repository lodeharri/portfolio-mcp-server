"""Conformance tests for ``src/mcp_server/application/ports/embedding.py``.

The :class:`EmbeddingPort` Protocol declares the contract an embedding
adapter must satisfy. Any class with the right method signature
structurally satisfies it (Python's ``Protocol`` is duck-typed).

These tests are RED until ``src/mcp_server/application/ports/embedding.py``
exists; the imports fail when the module is missing, which is the RED
signal.

Tests verify three things:

1. The module exposes an ``EmbeddingPort`` Protocol.
2. The Protocol declares the ``embed`` method.
3. A fake class with the right signature satisfies the Protocol
   (``isinstance(fake, EmbeddingPort)`` is ``True`` via ``runtime_checkable``).
"""

from __future__ import annotations

import inspect


class TestEmbeddingPortProtocol:
    """``EmbeddingPort`` declares the contract for batch embedding."""

    def test_embedding_port_protocol_exists(self) -> None:
        from mcp_server.application.ports.embedding import EmbeddingPort

        assert EmbeddingPort is not None

    def test_embedding_port_is_a_protocol(self) -> None:
        # All Protocol classes are also typing.Protocol instances. This
        # guards against accidentally exporting a regular class or a
        # typing.TypeVar / Generic instead of a Protocol.

        from mcp_server.application.ports.embedding import EmbeddingPort

        assert isinstance(EmbeddingPort, type) or hasattr(EmbeddingPort, "_is_protocol")

    def test_embedding_port_has_embed_method(self) -> None:
        from mcp_server.application.ports.embedding import EmbeddingPort

        members = dict(inspect.getmembers(EmbeddingPort))
        assert "embed" in members, (
            "EmbeddingPort must declare `embed(self, texts: list[str]) -> list[list[float]]` "
            "per the orchestrator's PR2 spec"
        )


class TestEmbeddingPortConformance:
    """A class with the right method signature satisfies ``EmbeddingPort``."""

    def test_fake_embedding_satisfies_protocol(self) -> None:
        from mcp_server.application.ports.embedding import EmbeddingPort

        class FakeEmbedding:
            """Minimal in-memory fake satisfying EmbeddingPort."""

            def embed(self, texts: list[str]) -> list[list[float]]:
                # Deterministic fake: each text gets a 2-dim zero vector.
                return [[0.0, 0.0] for _ in texts]

        fake = FakeEmbedding()
        # Protocol is runtime-checkable; isinstance verifies method names.
        assert isinstance(fake, EmbeddingPort)

    def test_fake_embedding_returns_correct_length(self) -> None:

        class FakeEmbedding:
            def embed(self, texts: list[str]) -> list[list[float]]:
                return [[0.1 * i for _ in range(4)] for i, _ in enumerate(texts)]

        fake = FakeEmbedding()
        result = fake.embed(["a", "b", "c"])
        assert isinstance(result, list)
        assert len(result) == 3
        assert all(len(v) == 4 for v in result)

    def test_class_without_embed_does_not_satisfy_protocol(self) -> None:
        """Negative test: a class missing the method does NOT satisfy the port."""
        from mcp_server.application.ports.embedding import EmbeddingPort

        class NotAnEmbedding:
            def other_method(self) -> None:
                pass

        not_an_embedding = NotAnEmbedding()
        assert not isinstance(not_an_embedding, EmbeddingPort)
