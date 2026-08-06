"""Vector store port — application-layer contract for chunk persistence.

Use cases (``PreindexUseCase``, ``SearchCodeUseCase``) depend on this
Protocol, never on the concrete ``SqliteVecStore``. The Protocol is
structural: any class with the right method signatures satisfies it.

``CodeChunk`` is a domain entity defined in change 003 (preindex pipeline).
PR2 forward-references it via ``TYPE_CHECKING`` so the port module imports
cleanly without depending on PR3 entities. The port signature uses
``list["CodeChunk"]`` as a string annotation that resolves at type-check
time but is just ``list`` at runtime — keeping PR2 scoped correctly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from mcp_server.domain.entities import CodeChunk


@runtime_checkable
class VectorStorePort(Protocol):
    """Contract for any chunk persistence adapter.

    Three operations:

    * ``has_hash`` — cache lookup for idempotent preindex (ADR-004).
    * ``upsert`` — batch insert of new chunks (chunk_hash UNIQUE).
    * ``search`` — top-k vector similarity over a single embedding dim.
    """

    def has_hash(self, chunk_hash: str) -> bool:
        """Return ``True`` if ``chunk_hash`` is already persisted.

        Used by the preindex pipeline to skip re-embedding chunks that
        already exist in the index. The hash includes
        ``embedding_dim`` per ADR-004 so a dim-change invalidates the
        cache automatically.
        """
        ...

    def upsert(self, chunks: list[CodeChunk]) -> None:
        """Persist a batch of chunks.

        Implementations MUST be idempotent on ``chunk_hash`` (re-inserting
        the same hash is a no-op, NOT an error). Flagged chunks
        (``chunk.flagged == True``) are stored alongside clean chunks so
        downstream callers can filter them out.
        """
        ...

    def search(self, query_vector: list[float], limit: int = 10) -> list[CodeChunk]:
        """Top-k vector similarity search.

        Returns up to ``limit`` chunks ordered by ascending distance to
        ``query_vector``. An empty list is returned when the index is
        empty or the query exceeds the dim mismatch (sqlite-vec contract).
        """
        ...

    def count_by_project(self, project_id: str) -> int:
        """Return the number of indexed chunks for ``project_id``.

        Used by the ``list_projects`` MCP tool to surface
        ``index_chunk_count`` per declared project. Implementations
        MUST return ``0`` when no chunks exist for the project (not
        raise). Implementations MUST be O(1) or at most O(log N) on
        the ``code_chunks`` index — the call is on the hot path of
        every ``list_projects`` invocation.
        """
        ...

    def distinct_file_paths(self, project_id: str) -> set[str]:
        """Return the set of distinct ``file_path`` values for ``project_id``.

        Used by the preindex CLI's ``--purge-orphans`` flag to detect
        files in the DB that no longer exist on disk. Implementations
        MUST return an empty set when no chunks exist for the project.
        """
        ...

    def delete_by_file_path(self, project_id: str, file_path: str) -> int:
        """Delete all chunks for ``(project_id, file_path)``.

        Returns the number of rows deleted (across both the text and
        vector tables). Used by the preindex CLI's ``--purge-orphans``
        flag to remove stale chunks after a file is deleted from the
        project tree.
        """
        ...


__all__ = ["VectorStorePort"]
