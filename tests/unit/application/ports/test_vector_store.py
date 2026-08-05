"""Conformance tests for ``src/mcp_server/application/ports/vector_store.py``.

The :class:`VectorStorePort` Protocol declares the contract a vector store
adapter must satisfy. Per the orchestrator's PR2 spec, it has three
methods:

* ``has_hash(chunk_hash: str) -> bool``
* ``upsert(chunks: list[CodeChunk]) -> None``
* ``search(query_vector: list[float], limit: int = 10) -> list[CodeChunk]``

``CodeChunk`` is a domain entity defined in PR3; for PR2 the port uses a
``TYPE_CHECKING`` forward reference so the port module imports cleanly
without PR3 dependencies.
"""

from __future__ import annotations

import inspect


class TestVectorStorePortProtocol:
    """``VectorStorePort`` declares the contract for chunk persistence."""

    def test_vector_store_port_protocol_exists(self) -> None:
        from mcp_server.application.ports.vector_store import VectorStorePort

        assert VectorStorePort is not None

    def test_vector_store_port_has_has_hash(self) -> None:
        from mcp_server.application.ports.vector_store import VectorStorePort

        members = dict(inspect.getmembers(VectorStorePort))
        assert "has_hash" in members

    def test_vector_store_port_has_upsert(self) -> None:
        from mcp_server.application.ports.vector_store import VectorStorePort

        members = dict(inspect.getmembers(VectorStorePort))
        assert "upsert" in members

    def test_vector_store_port_has_search(self) -> None:
        from mcp_server.application.ports.vector_store import VectorStorePort

        members = dict(inspect.getmembers(VectorStorePort))
        assert "search" in members


class TestVectorStorePortConformance:
    """A class with the right methods satisfies ``VectorStorePort``."""

    def test_fake_vector_store_satisfies_protocol(self) -> None:
        from mcp_server.application.ports.vector_store import VectorStorePort

        class FakeVectorStore:
            """In-memory fake satisfying VectorStorePort."""

            def __init__(self) -> None:
                self._chunks: dict[str, list[float]] = {}

            def has_hash(self, chunk_hash: str) -> bool:
                return chunk_hash in self._chunks

            def upsert(self, chunks: list) -> None:
                for chunk in chunks:
                    # chunk_hash is duck-typed — accept any object with the attr.
                    self._chunks[chunk.chunk_hash] = chunk.embedding

            def search(self, query_vector: list[float], limit: int = 10) -> list:
                return []

        fake = FakeVectorStore()
        assert isinstance(fake, VectorStorePort)

    def test_fake_vector_store_has_hash_behavior(self) -> None:

        class FakeVectorStore:
            _store: dict[str, list[float]] = {}  # noqa: RUF012 — shared mutable on class for the test fixture

            def has_hash(self, chunk_hash: str) -> bool:
                return chunk_hash in self._store

            def upsert(self, chunks: list) -> None:
                pass

            def search(self, query_vector: list[float], limit: int = 10) -> list:
                return []

        store = FakeVectorStore()
        assert store.has_hash("missing") is False

    def test_class_without_methods_does_not_satisfy_protocol(self) -> None:
        from mcp_server.application.ports.vector_store import VectorStorePort

        class NotAVectorStore:
            pass

        assert not isinstance(NotAVectorStore(), VectorStorePort)
