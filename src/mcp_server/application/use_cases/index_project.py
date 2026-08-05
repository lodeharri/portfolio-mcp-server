"""IndexProjectUseCase — application-layer orchestration of the preindex pipeline.

The use case is the ONLY place that wires the per-chunk pipeline:

1. Walk each declared project's ``include_subdirs`` (respecting
   ``exclude_subdirs`` and the manifest's global ``exclude_paths``).
2. Filter by the manifest's ``include_extensions``.
3. Chunk content (1500 / 200 characters from the manifest).
4. Compute the canonical ``chunk_hash`` (5-tuple per ADR-004).
5. Skip if the ``VectorStorePort`` already has the hash.
6. Scan via ``SecretScannerPort`` — ``BLOCKED`` → skip,
   ``FLAGGED`` → insert with ``flagged=True``.
7. Embed via ``EmbeddingPort``.
8. Upsert via ``VectorStorePort``.
9. Emit audit events: ``project.start``, ``chunk.cache_hit``,
   ``secret.blocked``, ``secret.flagged``, ``chunk.upserted``,
   ``project.done``.

The 0.1-second sleep between successful ``embed`` calls lives HERE,
not in the embedding adapter (per ADR-003 follow-up). The adapter
only sleeps on retries; this use case paces inter-call budget.

The use case returns an :class:`IndexResult` with counters so the
CLI summary line can be machine-readable.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from mcp_server.application.ports.embedding import EmbeddingPort
from mcp_server.application.ports.secret_scanner import ScanVerdict, SecretScannerPort
from mcp_server.application.ports.vector_store import VectorStorePort
from mcp_server.domain.value_objects import compute_chunk_hash

__all__ = ["IndexProjectUseCase", "IndexResult"]


# ---------------------------------------------------------------------------
# Protocol for the manifest dependency (kept structural so the use case
# has no direct dependency on the YAML adapter — same idea as the
# application ports).
# ---------------------------------------------------------------------------


@runtime_checkable
class _ManifestProjectsProtocol(Protocol):
    """The slice of :class:`ManifestPort` the use case actually uses."""

    def projects(self) -> list[object]: ...

    def is_path_indexed(self, path: Path) -> bool: ...


@dataclass
class IndexResult:
    """Counters returned by :meth:`IndexProjectUseCase.execute`.

    Fields:

    * ``processed`` — number of files walked.
    * ``embedded`` — number of chunks that were sent to the embedder.
    * ``upserted`` — number of chunks persisted to the vector store.
    * ``cache_hits`` — chunks skipped because the hash already existed.
    * ``blocked`` — chunks dropped because the secret scan returned
      ``BLOCKED``.
    * ``flagged`` — chunks persisted with ``flagged=True`` (medium
      confidence secret scan finding).
    * ``errors`` — list of exceptions raised during processing (file
      I/O errors, embed timeouts, etc.). Empty on a clean run.
    """

    processed: int = 0
    embedded: int = 0
    upserted: int = 0
    cache_hits: int = 0
    blocked: int = 0
    flagged: int = 0
    files_skipped: int = 0
    errors: list[Exception] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


# Default chunking parameters; overridden by manifest when provided.
DEFAULT_CHUNK_SIZE = 1500
DEFAULT_CHUNK_OVERLAP = 200
DEFAULT_INTER_CALL_SLEEP_SECONDS = 0.1


class IndexProjectUseCase:
    """Orchestrate the preindex pipeline for a single project.

    Args:
        manifest: Adapter exposing :meth:`projects()` and
            :meth:`is_path_indexed()`. Eagerly the YAML adapter; the
            protocol above makes this testable with simple fakes.
        embedding: ``EmbeddingPort`` (real or mock).
        vector_store: ``VectorStorePort``.
        scanner: ``SecretScannerPort``.
        audit: Audit logger with ``info(event, **fields)`` /
            ``warn(event, **fields)``.
        chunk_size: Characters per chunk (default 1500).
        chunk_overlap: Overlap between consecutive chunks (default 200).
        inter_call_sleep_seconds: Sleep between consecutive successful
            ``embedding_port.embed`` calls (default 0.1 s — Gemini free
            tier RPM). Tests can set to 0 for fast runs.
    """

    def __init__(
        self,
        *,
        manifest: _ManifestProjectsProtocol,
        embedding: EmbeddingPort,
        vector_store: VectorStorePort,
        scanner: SecretScannerPort,
        audit: object,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
        inter_call_sleep_seconds: float = DEFAULT_INTER_CALL_SLEEP_SECONDS,
    ) -> None:
        self.manifest = manifest
        self.embedding = embedding
        self.vector_store = vector_store
        self.scanner = scanner
        self.audit = audit
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.inter_call_sleep_seconds = inter_call_sleep_seconds
        # Counter used to pace the 0.1 s sleep between *every* embed
        # call (across batches). Reset on each ``execute()`` invocation
        # so multiple ``execute`` calls don't accumulate sleeps.
        self._embed_call_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def execute(self, project_id: str) -> IndexResult:
        """Run the preindex pipeline for one declared project.

        Returns:
            :class:`IndexResult` with the per-run counters. The CLI
            renders this as a JSON line on stdout.
        """

        result = IndexResult()
        project = self._find_project(project_id)
        # Reset the inter-call sleep counter so multiple ``execute``
        # calls don't accumulate sleeps (each run stands alone).
        self._embed_call_count = 0
        self._audit("info", "project.start", project_id=project_id)

        for file_path in self._walk_project(project):
            result.processed += 1
            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                result.files_skipped += 1
                self._audit(
                    "warn",
                    "file.skipped",
                    source=str(file_path),
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if not content.strip():
                continue

            for chunk_text, start_char, end_char in self._chunk_content(content):
                code_chunk = self._build_chunk(
                    project_id=project_id,
                    file_path=file_path,
                    start_char=start_char,
                    end_char=end_char,
                    content=chunk_text,
                )
                if self.vector_store.has_hash(code_chunk.chunk_hash):
                    result.cache_hits += 1
                    self._audit(
                        "info",
                        "chunk.cache_hit",
                        project_id=project_id,
                        source=str(file_path),
                        chunk_hash=code_chunk.chunk_hash,
                    )
                    continue

                verdict = self.scanner.scan(
                    chunk_text, source=str(file_path)
                )
                if verdict is ScanVerdict.BLOCKED:
                    result.blocked += 1
                    self._audit(
                        "warn",
                        "secret.blocked",
                        source=str(file_path),
                        chunk_hash=code_chunk.chunk_hash,
                    )
                    continue

                flagged = verdict is ScanVerdict.FLAGGED
                if flagged:
                    result.flagged += 1
                    self._audit(
                        "warn",
                        "secret.flagged",
                        source=str(file_path),
                        chunk_hash=code_chunk.chunk_hash,
                    )

                embedding_dim = getattr(self.embedding, "embedding_dim", 768)
                # One embed call per chunk (Gemini SDK is per-text).
                vectors = self._embed_with_pacing(
                    [chunk_text], embedding_dim=embedding_dim
                )
                vector = vectors[0]
                result.embedded += 1

                chunk_to_persist = code_chunk.__class__(
                    chunk_hash=code_chunk.chunk_hash,
                    project_id=project_id,
                    file_path=str(file_path),
                    start_char=start_char,
                    end_char=end_char,
                    content=chunk_text,
                    embedding=vector,
                    embedding_dim=embedding_dim,
                    flagged=flagged,
                )
                try:
                    self.vector_store.upsert([chunk_to_persist])
                except Exception as exc:
                    result.errors.append(exc)
                    self._audit(
                        "warn",
                        "chunk.upsert_error",
                        source=str(file_path),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue
                result.upserted += 1
                self._audit(
                    "info",
                    "chunk.upserted",
                    project_id=project_id,
                    source=str(file_path),
                    chunk_hash=code_chunk.chunk_hash,
                    flagged=flagged,
                )

        self._audit("info", "project.done", project_id=project_id)
        return result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_project(self, project_id: str) -> object:
        projects = self.manifest.projects()
        for p in projects:
            if getattr(p, "id", None) == project_id:
                return p
        from mcp_server.domain.exceptions import ManifestProjectNotFoundError

        raise ManifestProjectNotFoundError(
            f"project {project_id!r} not declared in manifest"
        )

    def _walk_project(self, project: object) -> Iterable[Path]:
        """Walk the project root, honoring ``is_path_indexed`` (Layer 1)."""
        project_path = Path(project.path)
        # Layer 1: only walk the project's declared ``include_subdirs``.
        include = getattr(project, "include_subdirs", None) or ["."]
        for sub in include:
            sub_path = project_path / sub if sub != "." else project_path
            if not sub_path.exists():
                continue
            for file_path in sorted(sub_path.rglob("*")):
                if not file_path.is_file():
                    continue
                if not self.manifest.is_path_indexed(file_path):
                    continue
                yield file_path

    def _chunk_content(
        self,
        content: str,
    ) -> Iterable[tuple[str, int, int]]:
        """Yield ``(text, start_char, end_char)`` tuples.

        Splits ``content`` into ``chunk_size`` character windows with
        ``chunk_overlap`` character overlap on each consecutive pair.
        Empty content yields nothing.
        """
        if not content:
            return
        step = max(1, self.chunk_size - self.chunk_overlap)
        i = 0
        while i < len(content):
            end = min(i + self.chunk_size, len(content))
            yield content[i:end], i, end
            if end >= len(content):
                return
            i += step

    def _embed_with_pacing(
        self,
        texts: list[str],
        *,
        embedding_dim: int,
    ) -> list[list[float]]:
        """Embed with the 0.1 s inter-call sleep between successive calls.

        The preindex use case embeds ONE chunk at a time (the adapter's
        ``embed`` accepts a batch but we iterate one-text-per-call so
        the inter-call sleep is meaningful). The 0.1 s pace applies
        only between *successful* calls — retries sleep via the
        adapter's own policy.

        Sleep semantics: the sleep fires before every embed call after
        the first in this use-case-lifetime counter. So two chunks
        embedded back-to-back yield exactly one 0.1 s sleep in between.
        """
        vectors: list[list[float]] = []
        for text in texts:
            self._embed_call_count += 1
            if self._embed_call_count > 1 and self.inter_call_sleep_seconds > 0:
                time.sleep(self.inter_call_sleep_seconds)
            partial = self.embedding.embed([text])
            vectors.extend(partial)
        return vectors

    def _build_chunk(
        self,
        *,
        project_id: str,
        file_path: Path,
        start_char: int,
        end_char: int,
        content: str,
    ) -> object:
        """Build a :class:`CodeChunk` with hash + placeholder embedding.

        The actual embedding arrives later from ``_embed_with_pacing``;
        we use a zero placeholder here so the chunk object exists for
        ``vector_store.has_hash`` queries and audit events.
        """
        from mcp_server.domain.entities import CodeChunk

        embedding_dim = getattr(self.embedding, "embedding_dim", 768)
        chunk_hash = compute_chunk_hash(
            project_id=project_id,
            file_path=str(file_path),
            start_char=start_char,
            content=content,
            embedding_dim=embedding_dim,
        )
        return CodeChunk(
            chunk_hash=chunk_hash,
            project_id=project_id,
            file_path=str(file_path),
            start_char=start_char,
            end_char=end_char,
            content=content,
            embedding=[0.0] * embedding_dim,
            embedding_dim=embedding_dim,
        )

    def _audit(self, level: str, event: str, **fields: object) -> None:
        if self.audit is None:
            return
        emit = getattr(self.audit, level, None)
        if callable(emit):
            emit(event, **fields)
