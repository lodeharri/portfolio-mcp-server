"""Domain entities — Pydantic v2 frozen models (pure Python, no framework deps).

Three entities live here per the preindex-pipeline spec:

* :class:`CodeChunk` — a single embedded text segment with optional
  gitleaks flag. Persisted to ``code_chunks`` and ``vec_chunks_<dim>``.
* :class:`Project` — the application-layer view of a manifest project
  entry. Re-exported via :mod:`mcp_server.application.ports.manifest`
  for backward compatibility with PR2.
* :class:`SearchResult` — a single vector-search hit returned by
  ``VectorStorePort.search``.

All models are ``frozen=True`` so they cannot be mutated after
construction (the preindex use case relies on this — chunks are
immutable once persisted).
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mcp_server.domain.value_objects import compute_chunk_hash

__all__ = ["CodeChunk", "Project", "SearchResult"]


# ---------------------------------------------------------------------------
# CodeChunk
# ---------------------------------------------------------------------------


class CodeChunk(BaseModel):
    """A chunk of indexed source text with its embedding and metadata.

    Attributes:
        chunk_hash: SHA-256 hex digest of the canonical 5-tuple
            ``(project_id, file_path, start_char, content, embedding_dim)``
            — see ``mcp_server.domain.value_objects.compute_chunk_hash``
            and ADR-004.
        project_id: Originating project (from ``Manifest.projects[].id``).
        file_path: Absolute path of the file this chunk came from.
        start_char: Inclusive 0-based character offset of the chunk start.
        end_char: Exclusive 0-based character offset of the chunk end.
        content: The raw text of the chunk.
        embedding: 768-dimensional float vector returned by the
            EmbeddingPort.
        embedding_dim: Dimension of ``embedding``. Defaults to 768
            (the ``text-embedding-004`` dim) but the field carries
            the actual dim used at insertion time so a future dim
            change (e.g. 1024) does not corrupt query bounds.
        flagged: ``True`` when the gitleaks scan returned ``FLAGGED``
            (medium-confidence finding) — chunk IS still inserted.
            ``BLOCKED`` chunks never reach this entity.
    """

    model_config = ConfigDict(frozen=True)

    chunk_hash: str = Field(min_length=64, max_length=64)
    project_id: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(ge=0)
    content: str
    embedding: list[float] = Field(min_length=1)
    embedding_dim: int = Field(default=768, ge=1)
    flagged: bool = False

    def __init__(self, **data: object) -> None:
        # If the caller didn't compute the hash themselves, derive it
        # from the canonical tuple. Most call sites pass an explicit
        # chunk_hash because it lets the use case check the cache
        # BEFORE constructing the full object.
        if "chunk_hash" not in data:
            data["chunk_hash"] = compute_chunk_hash(
                project_id=str(data["project_id"]),
                file_path=str(data["file_path"]),
                start_char=int(data["start_char"]),  # type: ignore[arg-type]
                content=str(data["content"]),
                embedding_dim=int(data.get("embedding_dim", 768)),  # type: ignore[arg-type]
            )
        super().__init__(**data)


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class Project(BaseModel):
    """Application-layer view of a single declared project.

    Per the manifest loader contract (``YamlManifestAdapter``), this is
    the canonical shape returned by ``Manifest.projects`` and consulted
    by the preindex use case. The previous home of this class was
    ``mcp_server.application.ports.manifest`` — the class itself is
    re-exported there for backward compatibility with PR2 (the
    adapter imports kept that path stable).

    Attributes:
        id: Stable identifier (e.g. ``finance-coach-latam``).
        path: Absolute filesystem path to the project root.
        display_name: Human-readable name for the playground UI.
        description: Long-form description (markdown allowed).
        include_subdirs: Whitelist of top-level subdirectories to walk.
        exclude_subdirs: Blacklist of subdirectories to skip, applied on
            top of ``include_subdirs``.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1)
    path: Path
    display_name: str = ""
    description: str = ""
    include_subdirs: list[str] = Field(default_factory=list)
    exclude_subdirs: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------


class SearchResult(BaseModel):
    """A single vector-search hit returned to the MCP layer.

    Differs from :class:`CodeChunk` in two ways:

    * Has a ``score`` (the cosine distance returned by sqlite-vec) but
      no ``embedding`` — only the chunk text is returned to the client.
    * Has ``line_start``/``line_end`` (1-based line offsets) because
      search results reference code locations, not arbitrary text spans.

    Attributes:
        chunk_hash: SHA-256 hex of the canonical 5-tuple so callers
            can re-query or correlate the result back to its index row.
        file_path: Absolute path of the matched file.
        line_start: 1-based line number where the match begins.
        line_end: 1-based line number where the match ends.
        content: The matched chunk text.
        score: Cosine distance to the query vector. Lower is closer.
        project_id: Originating project.
    """

    model_config = ConfigDict(frozen=True)

    chunk_hash: str = Field(min_length=64, max_length=64)
    file_path: str = Field(min_length=1)
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    content: str
    score: float = Field(ge=0.0)
    project_id: str = Field(min_length=1)
