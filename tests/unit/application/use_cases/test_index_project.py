"""Unit tests for ``src/mcp_server/application/use_cases/index_project.py``.

The preindex use case orchestrates the indexing pipeline:

1. Load manifest via :class:`ManifestPort`.
2. Walk each declared project's ``include_subdirs`` (respecting
   ``exclude_subdirs`` and the manifest's global ``exclude_paths``).
3. Filter by the manifest's ``include_extensions``.
4. Chunk content (1500 / 200 from manifest).
5. Compute ``chunk_hash`` (canonical 5-tuple including ``embedding_dim``).
6. Skip if :meth:`VectorStorePort.has_hash` already has it.
7. Scan via :class:`SecretScannerPort` — ``BLOCKED`` → skip,
   ``FLAGGED`` → insert with ``flagged=True``.
8. Embed via :class:`EmbeddingPort`.
9. Upsert via :class:`VectorStorePort`.
10. Emit :class:`AuditLogger` events for cache hits, blocked, flagged,
    upserts, and errors.

The 0.1 s sleep between successful embed calls lives HERE, not in
the adapter (per ADR-003 follow-up).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_server.application.ports.secret_scanner import ScanVerdict


def _fake_embed(text: str, dim: int = 768) -> list[float]:
    """Deterministic embedding — used by the test FakeEmbeddingPort."""
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Stretch the 32-byte digest to ``dim`` floats via repeat hashing.
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        chunk = hashlib.sha256(h + counter.to_bytes(4, "big")).digest()
        out.extend(int.from_bytes(chunk[i : i + 4], "big") / 2**32 for i in range(0, 16, 4))
        counter += 1
    return out[:dim]


class _FakeManifestEntry:
    def __init__(
        self,
        *,
        id: str,
        path: str,
        include_subdirs: list[str],
        exclude_subdirs: list[str] | None = None,
    ) -> None:
        self.id = id
        self.path = Path(path)
        self.include_subdirs = include_subdirs
        self.exclude_subdirs = exclude_subdirs or []
        self.display_name = id
        self.description = ""


class _FakeManifestPort:
    """In-memory ManifestPort returning a configurable list of projects."""

    def __init__(self, projects: list[_FakeManifestEntry]) -> None:
        self._projects = projects

    def load(self) -> object:
        return None

    def projects(self) -> list[_FakeManifestEntry]:
        return list(self._projects)

    def is_path_indexed(self, path: Path) -> bool:
        return True


class _FakeVectorStore:
    def __init__(self) -> None:
        self._store: dict[str, list[float]] = {}
        self.upserts: list[list[Any]] = []

    def has_hash(self, chunk_hash: str) -> bool:
        return chunk_hash in self._store

    def upsert(self, chunks: list[Any]) -> None:
        for chunk in chunks:
            self._store[chunk.chunk_hash] = chunk.embedding
        self.upserts.append(chunks)

    def search(self, query_vector: list[float], limit: int = 10) -> list[Any]:
        return []


class _FakeEmbeddingPort:
    def __init__(self, dim: int = 768) -> None:
        self.calls: list[list[str]] = []
        self.dim = dim
        self.embedding_dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [_fake_embed(t, self.dim) for t in texts]


class _FakeScanner:
    def __init__(self, verdict: ScanVerdict = ScanVerdict.CLEAN) -> None:
        self._verdict = verdict
        self.calls: list[tuple[str, str]] = []

    def scan(self, content: str, source: str) -> ScanVerdict:
        self.calls.append((content, source))
        return self._verdict


class _FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def warn(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def _make_project_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a tiny file tree for the use case to walk."""
    root = tmp_path / "proj"
    root.mkdir()
    for relpath, content in files.items():
        full = root / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return root


def _build_use_case(
    *,
    projects: list[_FakeManifestEntry],
    files: dict[str, str],
    tmp_path: Path,
    embedding: _FakeEmbeddingPort | None = None,
    vector_store: _FakeVectorStore | None = None,
    scanner: _FakeScanner | None = None,
    audit: _FakeAudit | None = None,
    inter_call_sleep_seconds: float = 0.0,
    chunk_size: int = 1500,
    chunk_overlap: int = 200,
):
    """Construct an ``IndexProjectUseCase`` with all ports mocked + a tiny file tree."""
    from mcp_server.application.use_cases.index_project import (
        IndexProjectUseCase,
    )

    root = _make_project_tree(tmp_path, files)

    manifest = _FakeManifestPort(projects)
    embedding = embedding or _FakeEmbeddingPort()
    vector_store = vector_store or _FakeVectorStore()
    scanner = scanner or _FakeScanner()
    audit = audit or _FakeAudit()

    uc = IndexProjectUseCase(
        manifest=manifest,
        embedding=embedding,
        vector_store=vector_store,
        scanner=scanner,
        audit=audit,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        inter_call_sleep_seconds=inter_call_sleep_seconds,
    )
    return uc, manifest, embedding, vector_store, scanner, audit, root


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestIndexProjectHappyPath:
    """The pipeline embeds every chunk and inserts it."""

    def test_indexes_two_files_into_three_chunks(self, tmp_path: Path) -> None:
        project = _FakeManifestEntry(
            id="p1", path=str(tmp_path / "proj"), include_subdirs=["."]
        )
        # 1500 char file → 1 chunk; 3000 char file → 2 chunks.
        files = {
            "a.py": "x" * 1500,
            "b.py": "y" * 3000,
        }
        uc, _, embedding, vec, _, _, _ = _build_use_case(
            projects=[project], files=files, tmp_path=tmp_path,
        )

        result = uc.execute("p1")

        assert result.processed == 2
        # 1 + 2 = 3 chunks
        assert result.embedded == 3
        assert result.upserted == 3
        assert result.cache_hits == 0
        assert len(vec._store) == 3
        # 3 embed calls (one per chunk).
        assert sum(len(c) for c in embedding.calls) == 3

    def test_returns_index_result_with_counts(self, tmp_path: Path) -> None:
        from mcp_server.application.use_cases.index_project import IndexResult

        project = _FakeManifestEntry(
            id="p1", path=str(tmp_path / "proj"), include_subdirs=["."]
        )
        files = {"a.py": "x" * 100}
        uc, *_ = _build_use_case(
            projects=[project], files=files, tmp_path=tmp_path,
        )

        result = uc.execute("p1")

        assert isinstance(result, IndexResult)
        assert result.processed == 1
        assert result.embedded == 0  # empty file → no chunks
        assert result.upserted == 0


# ---------------------------------------------------------------------------
# Idempotency / cache hits
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Re-running with no changes is a no-op for the cache."""

    def test_second_run_has_zero_new_embeddings(self, tmp_path: Path) -> None:
        project = _FakeManifestEntry(
            id="p1", path=str(tmp_path / "proj"), include_subdirs=["."]
        )
        files = {"a.py": "x" * 1500}
        uc, _, embedding, _, _, _, _ = _build_use_case(
            projects=[project], files=files, tmp_path=tmp_path,
        )
        uc.execute("p1")
        first_calls = sum(len(c) for c in embedding.calls)

        # Second run on the same DB → all cache hits.
        uc.execute("p1")
        second_calls = sum(len(c) for c in embedding.calls) - first_calls
        assert second_calls == 0

    def test_empty_file_yields_no_chunks(self, tmp_path: Path) -> None:
        project = _FakeManifestEntry(
            id="p1", path=str(tmp_path / "proj"), include_subdirs=["."]
        )
        uc, *_ = _build_use_case(
            projects=[project], files={"empty.py": ""}, tmp_path=tmp_path,
        )
        result = uc.execute("p1")
        assert result.embedded == 0
        assert result.upserted == 0


# ---------------------------------------------------------------------------
# Secret scan integration
# ---------------------------------------------------------------------------


class TestSecretScanIntegration:
    """BLOCKED → skip; FLAGGED → insert with flagged=True."""

    def test_blocked_chunk_does_not_upsert(self, tmp_path: Path) -> None:
        project = _FakeManifestEntry(
            id="p1", path=str(tmp_path / "proj"), include_subdirs=["."]
        )
        scanner = _FakeScanner(verdict=ScanVerdict.BLOCKED)
        uc, _, embedding, vec, scanner, audit, _ = _build_use_case(
            projects=[project],
            files={"a.py": "AKIAIOSFODNN7EXAMPLE secret"},
            tmp_path=tmp_path,
            scanner=scanner,
        )
        result = uc.execute("p1")

        assert result.blocked == 1
        assert result.upserted == 0
        assert sum(len(c) for c in embedding.calls) == 0
        assert len(vec._store) == 0
        # Audit MUST record secret.blocked.
        blocked = [e for e in audit.events if e[0] == "secret.blocked"]
        assert len(blocked) == 1

    def test_flagged_chunk_inserts_with_flagged_true(self, tmp_path: Path) -> None:
        project = _FakeManifestEntry(
            id="p1", path=str(tmp_path / "proj"), include_subdirs=["."]
        )
        scanner = _FakeScanner(verdict=ScanVerdict.FLAGGED)
        uc, *_ = _build_use_case(
            projects=[project],
            files={"a.py": "leaky-looking stuff"},
            tmp_path=tmp_path,
            scanner=scanner,
        )
        result = uc.execute("p1")

        assert result.flagged == 1
        assert result.upserted == 1

    def test_clean_chunk_inserts_normally(self, tmp_path: Path) -> None:
        project = _FakeManifestEntry(
            id="p1", path=str(tmp_path / "proj"), include_subdirs=["."]
        )
        scanner = _FakeScanner(verdict=ScanVerdict.CLEAN)
        uc, _, _, _, _, audit, _ = _build_use_case(
            projects=[project],
            files={"a.py": "import os"},
            tmp_path=tmp_path,
            scanner=scanner,
        )
        uc.execute("p1")
        # No secret events for the CLEAN path.
        assert not any(e[0] in {"secret.blocked", "secret.flagged"} for e in audit.events)


# ---------------------------------------------------------------------------
# 0.1 s sleep between embed calls (ADR-003 follow-up)
# ---------------------------------------------------------------------------


class TestInterCallSleep:
    """The use case MUST sleep between successive embed calls."""

    def test_sleeps_between_consecutive_embed_calls(self, tmp_path: Path) -> None:
        project = _FakeManifestEntry(
            id="p1", path=str(tmp_path / "proj"), include_subdirs=["."]
        )
        files = {"a.py": "x" * 1500, "b.py": "y" * 1500}
        uc, *_ = _build_use_case(
            projects=[project],
            files=files,
            tmp_path=tmp_path,
            inter_call_sleep_seconds=0.0,  # no real sleep
        )
        # Patch time.sleep on the use-case module to capture call args.
        from mcp_server.application.use_cases import index_project as ip

        sleeps: list[float] = []
        original_sleep = ip.time.sleep
        ip.time.sleep = lambda s: sleeps.append(s)
        try:
            uc.execute("p1")
        finally:
            ip.time.sleep = original_sleep

        # Two embed calls → one inter-call sleep between them.
        assert sum(1 for s in sleeps if s == 0.1) == 1

    def test_no_inter_call_sleep_when_single_chunk(self, tmp_path: Path) -> None:
        project = _FakeManifestEntry(
            id="p1", path=str(tmp_path / "proj"), include_subdirs=["."]
        )
        files = {"a.py": "x" * 1500}  # exactly one chunk
        uc, *_ = _build_use_case(
            projects=[project], files=files, tmp_path=tmp_path,
            inter_call_sleep_seconds=0.0,
        )
        from mcp_server.application.use_cases import index_project as ip

        sleeps: list[float] = []
        original_sleep = ip.time.sleep
        ip.time.sleep = lambda s: sleeps.append(s)
        try:
            uc.execute("p1")
        finally:
            ip.time.sleep = original_sleep

        # No inter-call sleeps (single chunk — no "between" calls).
        assert not any(s == 0.1 for s in sleeps)


# ---------------------------------------------------------------------------
# Manifest-level defaults
# ---------------------------------------------------------------------------


class TestManifestManifestDefaults:
    """Default manifest is honored and 0.1 s sleep is the default."""

    def test_default_inter_call_sleep_is_0p1_seconds(self) -> None:
        from mcp_server.application.use_cases.index_project import (
            IndexProjectUseCase,
        )

        uc = IndexProjectUseCase(
            manifest=MagicMock(),
            embedding=MagicMock(),
            vector_store=MagicMock(),
            scanner=MagicMock(),
            audit=MagicMock(),
        )
        assert uc.inter_call_sleep_seconds == 0.1
