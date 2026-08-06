"""Unit tests for ``src/mcp_server/infrastructure/adapters/sqlite_vec_store.py``.

The vec store adapter implements :class:`VectorStorePort`:

* ``has_hash(chunk_hash) -> bool``
* ``upsert(chunks: list[CodeChunk]) -> None``
* ``search(query_vector: list[float], limit=10) -> list[CodeChunk]``

It uses sqlite-vec's ``vec0`` virtual table for cosine-distance search
and per ADR-004 the table is named ``vec_chunks_{embedding_dim}``. For
001-bootstrap only the ``vec_chunks_768`` table exists.

Tests use the in-memory connection helper so they do not touch the
filesystem.
"""

from __future__ import annotations

import struct

import pytest

from mcp_server.domain.entities import CodeChunk


def _pack(vec: list[float]) -> bytes:
    """Pack a list of floats into the sqlite-vec binary blob."""
    return struct.pack(f"{len(vec)}f", *vec)


def _make_chunk(
    *,
    chunk_hash: str,
    project_id: str = "p",
    file_path: str = "/tmp/p/foo.py",
    start_char: int = 0,
    end_char: int = 10,
    content: str = "x = 1",
    embedding: list[float] | None = None,
    embedding_dim: int = 768,
    flagged: bool = False,
) -> CodeChunk:
    return CodeChunk(
        chunk_hash=chunk_hash,
        project_id=project_id,
        file_path=file_path,
        start_char=start_char,
        end_char=end_char,
        content=content,
        embedding=embedding if embedding is not None else [0.0] * embedding_dim,
        embedding_dim=embedding_dim,
        flagged=flagged,
    )


# ---------------------------------------------------------------------------
# Construction + connection lifecycle
# ---------------------------------------------------------------------------


class TestSqliteVecStoreContract:
    """``SqliteVecStore`` opens a sqlite-vec connection on demand."""

    def test_can_be_imported(self) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )

        assert SqliteVecStore is not None

    def test_satisfies_vector_store_port_protocol(self, tmp_path) -> None:
        """The adapter MUST satisfy :class:`VectorStorePort`."""
        from mcp_server.application.ports.vector_store import VectorStorePort
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        db_path = tmp_path / "test.sqlite"
        store = SqliteVecStore(open_db(db_path))
        try:
            assert isinstance(store, VectorStorePort)
        finally:
            store.close()

    def test_close_releases_connection(self, tmp_path) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        db_path = tmp_path / "test.sqlite"
        store = SqliteVecStore(open_db(db_path))
        # close should not raise.
        store.close()


# ---------------------------------------------------------------------------
# has_hash
# ---------------------------------------------------------------------------


class TestHasHash:
    """``has_hash`` answers the cache lookup question."""

    def test_returns_false_for_empty_store(self, tmp_path) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            assert store.has_hash("nonexistent") is False
        finally:
            store.close()

    def test_returns_true_after_upsert(self, tmp_path) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            chunk = _make_chunk(chunk_hash="a" * 64)
            assert store.has_hash(chunk.chunk_hash) is False
            store.upsert([chunk])
            assert store.has_hash(chunk.chunk_hash) is True
        finally:
            store.close()


# ---------------------------------------------------------------------------
# upsert
# ---------------------------------------------------------------------------


class TestUpsert:
    """``upsert`` writes metadata + vec rows keyed by ``chunk_hash``."""

    def test_upsert_inserts_code_chunks_row(self, tmp_path) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            chunk = _make_chunk(
                chunk_hash="a" * 64,
                project_id="finance-coach-latam",
                content="def hello(): pass",
            )
            store.upsert([chunk])
            cur = store._conn.execute(
                "SELECT chunk_hash, project_id, flagged FROM code_chunks WHERE chunk_hash=?",
                (chunk.chunk_hash,),
            )
            row = cur.fetchone()
            assert row is not None
            assert row[1] == "finance-coach-latam"
            assert row[2] == 0
        finally:
            store.close()

    def test_upsert_inserts_vec_chunks_768_row(self, tmp_path) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            chunk = _make_chunk(chunk_hash="b" * 64)
            store.upsert([chunk])
            cur = store._conn.execute(
                "SELECT chunk_hash FROM vec_chunks_768 WHERE chunk_hash=?",
                (chunk.chunk_hash,),
            )
            assert cur.fetchone() is not None
        finally:
            store.close()

    def test_upsert_idempotent_on_same_hash(self, tmp_path) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            chunk = _make_chunk(chunk_hash="c" * 64)
            store.upsert([chunk])
            store.upsert([chunk])
            cur = store._conn.execute(
                "SELECT count(*) FROM code_chunks WHERE chunk_hash=?",
                (chunk.chunk_hash,),
            )
            assert cur.fetchone()[0] == 1
        finally:
            store.close()

    def test_upsert_marks_flagged(self, tmp_path) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            chunk = _make_chunk(chunk_hash="d" * 64, flagged=True)
            store.upsert([chunk])
            cur = store._conn.execute(
                "SELECT flagged FROM code_chunks WHERE chunk_hash=?",
                (chunk.chunk_hash,),
            )
            assert cur.fetchone()[0] == 1
        finally:
            store.close()


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    """``search`` runs cosine-distance over the 768-dim table."""

    def test_search_with_no_chunks_returns_empty_list(self, tmp_path) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            result = store.search([0.1] * 768)
            assert result == []
        finally:
            store.close()

    def test_search_returns_upserted_chunks(self, tmp_path) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            chunk = _make_chunk(
                chunk_hash="e" * 64,
                project_id="finance-coach-latam",
                content="hello world",
            )
            store.upsert([chunk])
            results = store.search([0.0] * 768, limit=5)
            assert len(results) == 1
            assert results[0].project_id == "finance-coach-latam"
        finally:
            store.close()

    def test_search_respects_limit(self, tmp_path) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            for i in range(10):
                store.upsert([_make_chunk(chunk_hash=f"{i:064x}")])
            results = store.search([0.0] * 768, limit=3)
            assert len(results) == 3
        finally:
            store.close()

    def test_search_results_include_distance_via_score(self, tmp_path) -> None:
        """``SearchResult.score`` MUST come from sqlite-vec's ``distance``."""
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            chunk = _make_chunk(chunk_hash="f" * 64)
            store.upsert([chunk])
            results = store.search([0.0] * 768, limit=5)
            assert len(results) == 1
            # When query is all zeros and chunk is all zeros, distance is 0.
            assert results[0].score == 0.0
        finally:
            store.close()

    def test_search_requires_768_dim_query(self, tmp_path) -> None:
        """A 1024-dim query against the 768 table MUST raise ``EmbeddingDimensionMismatchError``."""
        from mcp_server.domain.exceptions import EmbeddingDimensionMismatchError
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )
        from mcp_server.infrastructure.db.connection import open_db

        store = SqliteVecStore(open_db(tmp_path / "test.sqlite"))
        try:
            with pytest.raises(EmbeddingDimensionMismatchError):
                store.search([0.0] * 1024)
        finally:
            store.close()
