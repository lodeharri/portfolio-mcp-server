"""Domain value objects — pure types with no framework dependencies.

Value objects capture the *what* of the domain; entities (in
``mcp_server.domain.entities``) capture the *who* and the *has-a*:

* :class:`ChunkHash` — ``NewType`` over ``str`` — the SHA-256 hex
  digest of the canonical 5-tuple ``(project_id, file_path,
  start_char, content, embedding_dim)``. The ``compute_chunk_hash``
  helper is the single function that knows this tuple's shape; the
  preindex use case calls it once per chunk before any DB lookup.
* :class:`Vector` — ``BaseModel`` wrapping ``list[float]`` with a
  validator that enforces ``len(values) == embedding_dim``. Use it
  anywhere an embedding needs dim validation at construction time
  (the vector store adapter uses it to route by dim).
* :class:`Embedding` — semantic alias of :class:`Vector`. The two
  names mean the same thing today; the alias exists so call sites
  reading an "embedding" can use the semantically-loaded name.

The canonical hash tuple (per ADR-004) is::

    f"{project_id}|{file_path}|{start_char}|{embedding_dim}|{content}"

Note the order: ``embedding_dim`` comes BEFORE ``content`` so a future
embedding at a new dim produces a different hash even when the
content is identical. This is the dim-change scenario the spec
scenario "Re-embedding at a new dim produces new hashes" covers.
"""

from __future__ import annotations

import hashlib
from typing import NewType

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mcp_server.domain.exceptions import EmbeddingDimensionMismatchError

__all__ = [
    "ChunkHash",
    "Embedding",
    "Vector",
    "compute_chunk_hash",
]

# ---------------------------------------------------------------------------
# ChunkHash — NewType over str
# ---------------------------------------------------------------------------

# A NewType is intentional: it has no runtime cost and gives static
# type-checkers a hint that a parameter expecting a ChunkHash should
# receive one. The runtime check (64-char hex) lives in the entities
# module's CodeChunk model.
ChunkHash = NewType("ChunkHash", str)


# ---------------------------------------------------------------------------
# Vector — typed wrapper around list[float] with dim validation
# ---------------------------------------------------------------------------


class Vector(BaseModel):
    """A float vector with dim validation.

    Attributes:
        values: The raw floats (length must equal ``embedding_dim``).
        embedding_dim: Expected length. Stored alongside the values so
            downstream code (e.g. ``SqliteVecStore.search``) can route
            to the correct vec table without re-counting.

    Raises:
        EmbeddingDimensionMismatchError: when ``len(values) != embedding_dim``.
    """

    model_config = ConfigDict(frozen=True)

    values: list[float] = Field(min_length=1)
    embedding_dim: int = Field(ge=1)

    @model_validator(mode="after")
    def _enforce_dim(self) -> Vector:
        if len(self.values) != self.embedding_dim:
            raise EmbeddingDimensionMismatchError(
                f"vector length {len(self.values)} does not match "
                f"embedding_dim {self.embedding_dim}"
            )
        return self


# Embedding is a semantic alias for Vector. Same class — different name
# lets call sites choose the wording that fits their context.
Embedding = Vector


# ---------------------------------------------------------------------------
# compute_chunk_hash — canonical 5-tuple hash
# ---------------------------------------------------------------------------


def compute_chunk_hash(
    *,
    project_id: str,
    file_path: str,
    start_char: int,
    content: str,
    embedding_dim: int,
) -> str:
    """Return the SHA-256 hex digest of the canonical chunk tuple.

    The canonical tuple (per ADR-004) is::

        f"{project_id}|{file_path}|{start_char}|{embedding_dim}|{content}"

    ``embedding_dim`` appears BEFORE ``content`` so a future dim change
    (e.g. switching from ``text-embedding-004``'s 768 to 1024) produces
    a different hash even when content is unchanged. This guarantees
    chunks at different dims can coexist in the same store and the
    cache lookup never returns a stale row.

    Args:
        project_id: Originating project.
        file_path: File path (string form — converted at use sites).
        start_char: Starting character offset of the chunk.
        content: Chunk text.
        embedding_dim: Embedding dimension this chunk was embedded at.

    Returns:
        64-character lowercase hex string.
    """
    canonical = f"{project_id}|{file_path}|{start_char}|{embedding_dim}|{content}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
