"""``SearchCodeUseCase`` — application-layer ``search_code`` MCP tool.

The use case is the **primary entry point** of the recruiter demo. A
recruiter asks "where did I implement rate limiting?" and the tool
embeds the query, hits :class:`VectorStorePort.search`, and returns
the top-k matches with the chunk text, file path, line range, and
cosine distance.

Pipeline:

1. Validate ``query`` (non-empty after ``.strip()``) and ``top_k``
   (``1 <= top_k <= 50`` per the spec's resource cap).
2. Call :meth:`EmbeddingPort.embed([query])` to get one query vector.
3. Call :meth:`VectorStorePort.search(query_vector, limit=top_k)` to
   retrieve the top-k candidates (pre-sorted by ascending score).
4. Client-side filter by ``project_id`` (when provided).
5. Sanitize every chunk ``content`` through :class:`OutputSanitizer`
   (Layer 3 — metadata fields pass through unchanged).
6. Return a list of JSON-serializable dicts ready for the MCP layer.

Hexagonal contract
------------------

Depends ONLY on ports:

* :class:`mcp_server.application.ports.embedding.EmbeddingPort`
* :class:`mcp_server.application.ports.vector_store.VectorStorePort`
* :class:`mcp_server.security.output_sanitizer.OutputSanitizer`
* :class:`mcp_server.security.audit.AuditLogger`

No concrete adapter imports, no LLM chat (only embedding).

Error surface
-------------

* ``ValueError`` for empty / whitespace-only ``query`` (per spec,
  maps to JSON-RPC ``-32602`` via ``translate_tool_error``).
* ``ValueError`` for ``top_k > 50`` (resource cap; per spec maps to
  ``-32602``).
* :class:`GeminiTransientError` propagates unchanged from the
  embedding adapter (after its internal retries) so the wrapper
  layer can map it to ``-32603``.
* :class:`EmbeddingDimensionMismatchError` propagates unchanged from
  the vector store (also mapped to ``-32603``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp_server.application.ports.embedding import EmbeddingPort
from mcp_server.application.ports.vector_store import VectorStorePort
from mcp_server.domain.entities import SearchResult
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer

__all__ = ["SearchCodeRequest", "SearchCodeUseCase", "SearchResultDict"]


# Type alias for the per-result dict shape. Matches ``SearchResult``'s
# public fields (chunk_hash, file_path, line_start, line_end, content,
# score, project_id) without exposing the ``embedding`` vector.
SearchResultDict = dict[str, Any]


# Hard cap on ``top_k`` per the spec's "Top-k clamped to a maximum of
# 50 (above which the use case MUST raise ValueError to prevent
# resource exhaustion)" contract.
MAX_TOP_K: int = 50


@dataclass(frozen=True)
class SearchCodeRequest:
    """Inputs to :meth:`SearchCodeUseCase.execute`.

    Attributes:
        query: Natural-language query. MUST be non-empty after strip.
        top_k: Maximum results to return. Clamped at ``MAX_TOP_K``.
        project_id: Optional scope filter — when set, results are
            restricted to that project. ``None`` spans all projects.
    """

    query: str
    top_k: int = 10
    project_id: str | None = None


class SearchCodeUseCase:
    """Embed a query and run a top-k vector search with sanitized output.

    Args:
        embedding: :class:`EmbeddingPort` (real or mock). The use
            case embeds one query per ``execute()`` call — a batch of
            1 — to align with the Gemini SDK's per-text call shape.
        vector_store: :class:`VectorStorePort` over the preindex
            database. The use case calls ``search(query_vector, limit)``
            and trusts the adapter's ascending-score ordering.
        sanitizer: :class:`OutputSanitizer` (Layer 3).
        audit: :class:`AuditLogger` (Layer 5). The sanitizer emits
            ``output.redacted`` directly; the use case itself does
            not need to call ``audit.warn`` for the read-only path.
    """

    def __init__(
        self,
        *,
        embedding: EmbeddingPort,
        vector_store: VectorStorePort,
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
    ) -> None:
        self.embedding = embedding
        self.vector_store = vector_store
        self.sanitizer = sanitizer
        self.audit = audit

    def execute(self, request: SearchCodeRequest) -> list[SearchResultDict]:
        """Run the embed→search pipeline; return sanitized top-k results.

        Returns:
            ``list[SearchResultDict]`` — one dict per result with the
            keys: ``chunk_hash``, ``file_path``, ``line_start``,
            ``line_end``, ``content`` (sanitized), ``score``,
            ``project_id``. Empty list when the index is empty or
            no results match the project filter.

        Raises:
            ValueError: ``query`` is empty / whitespace-only, OR
                ``top_k > MAX_TOP_K``.
            GeminiTransientError: embedding failed after the adapter's
                internal retries (propagated unchanged for the
                wrapper to map to ``-32603``).
            EmbeddingDimensionMismatchError: query vector dim does
                not match any known ``vec_chunks_<dim>`` table.
        """
        # Step 1: input validation.
        if not request.query or not request.query.strip():
            raise ValueError("query must be a non-empty, non-whitespace string")
        if request.top_k < 1:
            raise ValueError(f"top_k must be >= 1 (got {request.top_k})")
        if request.top_k > MAX_TOP_K:
            raise ValueError(
                f"top_k must be <= {MAX_TOP_K} (got {request.top_k}); "
                "the use case caps top_k to prevent resource exhaustion"
            )

        # Step 2: embed the query (single-text batch).
        vectors = self.embedding.embed([request.query])
        if not vectors:
            # Defensive: a well-behaved embedder returns one vector per
            # input text, but guard against malformed responses.
            return []
        query_vector = vectors[0]

        # Step 3: vector search (adapter returns ascending-score order).
        candidates: list[SearchResult] = self.vector_store.search(
            query_vector, limit=request.top_k
        )

        # Step 4: client-side project filter.
        if request.project_id is not None:
            candidates = [c for c in candidates if c.project_id == request.project_id]

        # Step 5+6: build a sanitized JSON-serializable list.
        payload: list[SearchResultDict] = []
        for c in candidates:
            payload.append(
                {
                    "chunk_hash": c.chunk_hash,
                    "file_path": c.file_path,
                    "line_start": c.line_start,
                    "line_end": c.line_end,
                    "content": c.content,  # sanitized below
                    "score": c.score,
                    "project_id": c.project_id,
                }
            )

        # Sanitize only the ``content`` field — metadata cannot carry
        # secrets. Walk the list manually (rather than sanitize_json
        # over the whole list) so non-string fields pass through
        # without JSON round-trip.
        sanitized = self._sanitize_content_only(payload, source="search_code")
        return sanitized

    def _sanitize_content_only(
        self, payload: list[SearchResultDict], *, source: str
    ) -> list[SearchResultDict]:
        """Sanitize only the ``content`` field of each result.

        Walks the list once; for each dict, replaces ``content`` with
        the redacted text. All other fields (chunk_hash, file_path,
        line_start, line_end, score, project_id) pass through
        verbatim because they cannot carry secrets and serializing
        them through JSON would risk float precision loss on
        ``score``.
        """
        result: list[SearchResultDict] = []
        for entry in payload:
            new_entry = dict(entry)
            sanitized = self.sanitizer.sanitize(
                str(entry["content"]), source=source
            )
            new_entry["content"] = sanitized.redacted_text
            result.append(new_entry)
        return result
