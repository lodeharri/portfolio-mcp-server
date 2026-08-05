"""SQLite-vec vector-store adapter — implements :class:`VectorStorePort`.

Persists :class:`CodeChunk` rows to:

* ``code_chunks`` — metadata + canonical hash + ``embedding_dim`` column.
* ``vec_chunks_768`` (sqlite-vec ``vec0`` virtual table) — the
  768-dim float vectors keyed by ``chunk_hash``.

Per ADR-004:

* The vec table is named per ``embedding_dim`` — only ``vec_chunks_768``
  exists today (001-bootstrap scope).
* The ``chunk_hash`` canonical tuple includes ``embedding_dim`` so a
  dim change (e.g. switching from 768 to 1024) does not produce
  silent hash collisions. The ``code_chunks`` row carries the dim so
  mixed-dim corpora can coexist safely.
* Routing at search time is ``len(query_vec)`` → ``vec_chunks_{dim}``
  table.

This module is the only place that touches the SQLite schema directly.
The composition root owns the connection lifetime and closes it on
app shutdown; this class exposes :meth:`close` for tests and CLI
shutdown.
"""

from __future__ import annotations

import sqlite3
import struct
from typing import Any

from mcp_server.application.ports.vector_store import VectorStorePort
from mcp_server.domain.entities import CodeChunk, SearchResult
from mcp_server.domain.exceptions import EmbeddingDimensionMismatchError

__all__ = ["SqliteVecStore", "pack_vector"]


# ---------------------------------------------------------------------------
# Binary packing helpers
# ---------------------------------------------------------------------------


def pack_vector(values: list[float]) -> bytes:
    """Pack a Python ``list[float]`` to the sqlite-vec binary blob format.

    sqlite-vec expects ``float[N]`` columns as a packed binary blob of
    N 32-bit floats in the connection's native byte order (little
    endian on every platform we ship).
    """
    return struct.pack(f"{len(values)}f", *values)


# ---------------------------------------------------------------------------
# SqliteVecStore
# ---------------------------------------------------------------------------


class SqliteVecStore:
    """Concrete :class:`VectorStorePort` over a sqlite-vec DB.

    Args:
        conn: An open ``sqlite3.Connection`` with ``sqlite_vec.load(conn)``
            called and ``schema.sql`` applied. The composition root
            supplies the connection via :func:`mcp_server.infrastructure
            .db.connection.open_db`.
    """

    _TABLE_NAME_TEMPLATE = "vec_chunks_{dim}"

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        self._conn.close()

    # ------------------------------------------------------------------
    # VectorStorePort API
    # ------------------------------------------------------------------

    def has_hash(self, chunk_hash: str) -> bool:
        """Return ``True`` if a chunk with this hash is already persisted."""
        cur = self._conn.execute(
            "SELECT 1 FROM code_chunks WHERE chunk_hash = ? LIMIT 1",
            (chunk_hash,),
        )
        return cur.fetchone() is not None

    def upsert(self, chunks: list[CodeChunk]) -> None:
        """Persist a batch of chunks (idempotent on ``chunk_hash``).

        Each chunk is inserted into:

        * ``code_chunks`` — metadata (chunk_hash, project_id, file_path,
          start_char, end_char, content, embedding_dim, flagged).
        * ``vec_chunks_<dim>`` — the binary float vector.

        The two inserts are wrapped in a single transaction so a
        half-written chunk never lands in the index.
        """
        if not chunks:
            return

        # Group by dim so we can batch the per-dim vec inserts.
        try:
            with self._conn:  # implicit transaction
                meta_rows = []
                for chunk in chunks:
                    self._validate_dim(chunk)
                    meta_rows.append(
                        (
                            chunk.chunk_hash,
                            chunk.project_id,
                            chunk.file_path,
                            chunk.start_char,
                            chunk.end_char,
                            chunk.content,
                            chunk.embedding_dim,
                            1 if chunk.flagged else 0,
                        )
                    )
                self._conn.executemany(
                    "INSERT OR IGNORE INTO code_chunks "
                    "(chunk_hash, project_id, file_path, start_char, end_char, "
                    " content, embedding_dim, flagged) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    meta_rows,
                )
                # Per-dim vec table inserts. The vec0 virtual table
                # doesn't honour ``INSERT OR REPLACE`` reliably across
                # sqlite-vec versions, so we delete + insert. Wrapped
                # in the parent transaction so a mid-loop failure rolls
                # back both halves.
                for chunk in chunks:
                    table = self._table_name(chunk.embedding_dim)
                    vec_blob = pack_vector(chunk.embedding)
                    self._conn.execute(
                        f"DELETE FROM {table} WHERE chunk_hash = ?",
                        (chunk.chunk_hash,),
                    )
                    self._conn.execute(
                        f"INSERT INTO {table} (chunk_hash, embedding) VALUES (?, ?)",
                        (chunk.chunk_hash, vec_blob),
                    )
        except sqlite3.OperationalError as exc:
            raise EmbeddingDimensionMismatchError(
                f"failed to upsert chunks: {exc}"
            ) from exc

    def search(
        self,
        query_vector: list[float],
        limit: int = 10,
    ) -> list[SearchResult]:
        """Return up to ``limit`` chunks ordered by cosine distance.

        Routing rule (ADR-004): the dim of ``query_vector`` selects the
        vec table. ``len(query_vector) == 768`` queries the
        ``vec_chunks_768`` table; a query at any other dim raises
        :class:`EmbeddingDimensionMismatchError`.
        """
        dim = len(query_vector)
        if dim == 0:
            return []
        if dim != self._known_dim():
            raise EmbeddingDimensionMismatchError(
                f"query vector dim {dim} does not match any known vec_chunks_<dim> table"
            )
        table = self._table_name(dim)
        vec_blob = pack_vector(query_vector)

        cur = self._conn.execute(
            f"""
            SELECT cc.chunk_hash, cc.project_id, cc.file_path,
                   cc.start_char, cc.end_char, cc.content, cc.flagged,
                   v.distance
            FROM {table} v
            JOIN code_chunks cc ON cc.chunk_hash = v.chunk_hash
            WHERE v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
            LIMIT ?
            """,
            (vec_blob, limit, limit),
        )
        results: list[SearchResult] = []
        for row in cur.fetchall():
            (
                chunk_hash,
                project_id,
                file_path,
                start_char,
                end_char,
                content,
                flagged,  # noqa: F841
                distance,
            ) = row
            results.append(
                SearchResult(
                    chunk_hash=chunk_hash,
                    file_path=file_path,
                    line_start=1,  # SearchResult uses 1-based lines; chunks carry chars
                    line_end=1,
                    content=content,
                    score=float(distance),
                    project_id=project_id,
                )
            )
        return results

    def count_by_project(self, project_id: str) -> int:
        """Return the number of indexed chunks for ``project_id``.

        O(N) over the ``code_chunks`` table (no dedicated index yet —
        002-mcp-tools PR1 scope). The ``list_projects`` tool calls this
        once per declared project; for the demo (2 projects, <10K
        chunks each) the cost is negligible. A future optimization
        could add a composite index on ``(project_id, chunk_hash)``.

        Returns ``0`` for an unknown project — never raises.
        """
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM code_chunks WHERE project_id = ?",
            (project_id,),
        )
        row = cur.fetchone()
        if row is None:
            return 0
        return int(row[0])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _known_dim(self) -> int:
        """Return the dim of the bootstrapped vec table (``vec_chunks_768``).

        For 001-bootstrap we hard-code 768 because the schema only
        declares that one table. Future dims will be discovered by
        querying ``sqlite_master`` for ``LIKE 'vec_chunks_%'``.
        """
        return 768

    def _table_name(self, dim: int) -> str:
        return self._TABLE_NAME_TEMPLATE.format(dim=dim)

    @staticmethod
    def _validate_dim(chunk: CodeChunk) -> None:
        if len(chunk.embedding) != chunk.embedding_dim:
            raise EmbeddingDimensionMismatchError(
                f"chunk {chunk.chunk_hash} embedding length "
                f"{len(chunk.embedding)} != embedding_dim {chunk.embedding_dim}"
            )
