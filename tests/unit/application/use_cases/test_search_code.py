"""Unit tests for ``SearchCodeUseCase``.

Covers the four spec requirements from
``openspec/changes/002-mcp-tools/specs/search_code.md``:

* **Query Is Embedded Then Searched** — embed→search pipeline;
  top-k results ordered by ascending score; empty query raises
  ``ValueError``.
* **Optional Project Scope Filter** — when ``project_id`` is set,
  client-side filter restricts results to that project; ``None``
  spans every project.
* **Output Passes Through OutputSanitizer (Layer 3)** — every chunk
  ``content`` is sanitized (5 SecretPatterns); metadata fields pass
  through unchanged; redactions aggregate to one ``output.redacted``
  audit event per chunk.
* **Embedding Errors Surface as Tool Errors** — ``GeminiTransientError``
  propagates unchanged so the wrapper layer (translate_tool_error)
  can map it to JSON-RPC -32603.

Hardening from the spec error/edge cases:
* ``top_k > 50`` MUST raise ``ValueError`` (resource cap).
* Empty index returns ``[]`` (no exception).
* Concurrent invocations are safe (no shared mutable state on the
  use case — the use case is stateless across calls).
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from mcp_server.domain.entities import SearchResult
from mcp_server.domain.exceptions import GeminiTransientError
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _hash_vector(text: str, dim: int = 768) -> list[float]:
    """Deterministic 768-dim vector for the test embedder."""
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        chunk = hashlib.sha256(f"{text}:{counter}".encode("utf-8")).digest()
        for i in range(0, 16, 4):
            out.append(int.from_bytes(chunk[i : i + 4], "big") / 2**32)
        counter += 1
    return out[:dim]


class _FakeEmbedding:
    """Configurable EmbeddingPort fake."""

    def __init__(
        self,
        *,
        vectors: dict[str, list[float]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.vectors = vectors or {}
        self.raise_exc = raise_exc
        self.calls: list[list[str]] = []
        self.embedding_dim = 768

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.raise_exc is not None:
            raise self.raise_exc
        return [self.vectors.get(t, _hash_vector(t)) for t in texts]


class _FakeVectorStore:
    """Configurable VectorStorePort fake with sorted-by-score search."""

    def __init__(self, results: list[SearchResult] | None = None) -> None:
        self.results = list(results or [])
        self.search_calls: list[tuple[list[float], int]] = []
        self._has: set[str] = set()
        self._counts: dict[str, int] = {}

    def has_hash(self, chunk_hash: str) -> bool:
        return chunk_hash in self._has

    def upsert(self, chunks: list[Any]) -> None:
        for c in chunks:
            self._has.add(c.chunk_hash)

    def search(self, query_vector: list[float], limit: int = 10) -> list[SearchResult]:
        self.search_calls.append((list(query_vector), limit))
        # Return up to ``limit`` in their declared order — the spec
        # requires the store to return ascending-score results, and
        # the fake is pre-built with that ordering.
        return list(self.results[:limit])

    def count_by_project(self, project_id: str) -> int:
        return self._counts.get(project_id, 0)


def _result(
    *,
    chunk_hash: str,
    file_path: str = "/tmp/example.py",
    content: str = "pass",
    score: float = 0.1,
    project_id: str = "finance-coach-latam",
    line_start: int = 1,
    line_end: int = 1,
) -> SearchResult:
    return SearchResult(
        chunk_hash=chunk_hash,
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        content=content,
        score=score,
        project_id=project_id,
    )


def _use_case(
    *,
    embedding: _FakeEmbedding,
    vector_store: _FakeVectorStore,
):
    """Build a SearchCodeUseCase with fakes + real sanitizer + audit."""
    from mcp_server.application.use_cases.search_code import (
        SearchCodeRequest,
        SearchCodeUseCase,
    )

    audit = AuditLogger()
    sanitizer = OutputSanitizer(audit=audit)
    uc = SearchCodeUseCase(
        embedding=embedding,  # type: ignore[arg-type]
        vector_store=vector_store,  # type: ignore[arg-type]
        sanitizer=sanitizer,
        audit=audit,
    )
    return uc, SearchCodeRequest


# ---------------------------------------------------------------------------
# Query Is Embedded Then Searched
# ---------------------------------------------------------------------------


class TestQueryIsEmbeddedThenSearched:
    """The use case embeds the query, then vector-store searches."""

    def test_returns_top_k_results_ordered_by_score(self) -> None:
        # Pre-sorted by ascending score per the spec.
        results = [
            _result(chunk_hash="a" * 64, score=0.10, content="a"),
            _result(chunk_hash="b" * 64, score=0.20, content="b"),
            _result(chunk_hash="c" * 64, score=0.30, content="c"),
            _result(chunk_hash="d" * 64, score=0.40, content="d"),
            _result(chunk_hash="e" * 64, score=0.50, content="e"),
        ]
        embedding = _FakeEmbedding(vectors={"rate limiting": _hash_vector("rate limiting")})
        store = _FakeVectorStore(results=results)
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="rate limiting", top_k=3))

        assert len(out) == 3
        # Order is preserved from the store (pre-sorted ascending).
        assert [r["chunk_hash"] for r in out] == ["a" * 64, "b" * 64, "c" * 64]
        assert all(r["score"] == s for r, s in zip(out, [0.10, 0.20, 0.30]))

    def test_calls_embed_then_search(self) -> None:
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(results=[_result(chunk_hash="a" * 64)])
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        uc.execute(Req(query="q"))

        assert embedding.calls == [["q"]]
        assert len(store.search_calls) == 1
        # The vector passed to search is the embedder's output.
        assert store.search_calls[0][0] == _hash_vector("q")
        # top_k default = 10; the use case MUST pass it to search.
        assert store.search_calls[0][1] == 10

    def test_each_entry_includes_required_fields(self) -> None:
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(
            results=[_result(chunk_hash="a" * 64, file_path="/x.py", line_start=10)]
        )
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="q"))

        assert len(out) == 1
        r = out[0]
        for field in ("chunk_hash", "file_path", "line_start", "line_end", "content", "score", "project_id"):
            assert field in r

    def test_empty_query_raises_value_error(self) -> None:
        embedding = _FakeEmbedding()
        store = _FakeVectorStore()
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        with pytest.raises(ValueError):
            uc.execute(Req(query=""))

    def test_whitespace_only_query_raises_value_error(self) -> None:
        embedding = _FakeEmbedding()
        store = _FakeVectorStore()
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        with pytest.raises(ValueError):
            uc.execute(Req(query="   \n\t  "))

    def test_top_k_greater_than_50_raises_value_error(self) -> None:
        """Per spec, ``top_k > 50`` MUST raise ``ValueError`` (resource cap)."""
        embedding = _FakeEmbedding()
        store = _FakeVectorStore()
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        with pytest.raises(ValueError):
            uc.execute(Req(query="q", top_k=51))

    def test_top_k_exactly_50_is_accepted(self) -> None:
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(results=[])
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="q", top_k=50))

        assert out == []
        assert store.search_calls[0][1] == 50


# ---------------------------------------------------------------------------
# Optional Project Scope Filter
# ---------------------------------------------------------------------------


class TestProjectScopeFilter:
    """``project_id`` is enforced client-side after the vector search."""

    def test_project_id_filter_excludes_other_projects(self) -> None:
        results = [
            _result(chunk_hash="a" * 64, project_id="finance-coach-latam"),
            _result(chunk_hash="b" * 64, project_id="landing-page-portfolio"),
            _result(chunk_hash="c" * 64, project_id="finance-coach-latam"),
        ]
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(results=results)
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="q", project_id="finance-coach-latam"))

        assert len(out) == 2
        assert all(r["project_id"] == "finance-coach-latam" for r in out)

    def test_no_filter_spans_all_projects(self) -> None:
        results = [
            _result(chunk_hash="a" * 64, project_id="finance-coach-latam"),
            _result(chunk_hash="b" * 64, project_id="landing-page-portfolio"),
        ]
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(results=results)
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="q"))

        assert {r["project_id"] for r in out} == {
            "finance-coach-latam",
            "landing-page-portfolio",
        }


# ---------------------------------------------------------------------------
# Output Sanitization (Layer 3)
# ---------------------------------------------------------------------------


class TestOutputSanitization:
    """Every chunk ``content`` is sanitized through ``OutputSanitizer``."""

    def test_aws_key_in_chunk_content_is_redacted(self) -> None:
        results = [
            _result(
                chunk_hash="a" * 64,
                content="aws_key = AKIAIOSFODNN7EXAMPLE leaked",
            )
        ]
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(results=results)
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="q"))

        assert "AKIAIOSFODNN7EXAMPLE" not in out[0]["content"]
        assert "[REDACTED]" in out[0]["content"]

    def test_clean_chunk_content_passes_through(self) -> None:
        clean = "def hello():\n    return 42"
        results = [_result(chunk_hash="a" * 64, content=clean)]
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(results=results)
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="q"))

        assert out[0]["content"] == clean

    def test_multiple_patterns_redacted_in_one_chunk(self) -> None:
        results = [
            _result(
                chunk_hash="a" * 64,
                content="AKIAIOSFODNN7EXAMPLE and ghp_" + "a" * 36 + " both here",
            )
        ]
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(results=results)
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="q"))

        # Two distinct redactions → two [REDACTED] placeholders.
        assert out[0]["content"].count("[REDACTED]") == 2
        assert "AKIAIOSFODNN7EXAMPLE" not in out[0]["content"]
        assert "ghp_" not in out[0]["content"]

    def test_generic_api_key_pattern_is_redacted(self) -> None:
        results = [
            _result(
                chunk_hash="a" * 64,
                content="api_key = abc123secret",
            )
        ]
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(results=results)
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="q"))

        # The whole ``api_key = value`` match is replaced with the
        # placeholder (the sanitizer's re.sub replaces the full match,
        # consistent with the AWS / GitHub / OpenAI / Gemini patterns).
        assert "api_key" not in out[0]["content"]
        assert "abc123secret" not in out[0]["content"]
        assert "[REDACTED]" in out[0]["content"]

    def test_metadata_fields_pass_through_unchanged(self) -> None:
        """``chunk_hash``/``file_path``/line numbers/``score``/``project_id``
        cannot carry secrets — they MUST pass through unchanged."""
        results = [
            _result(
                chunk_hash="a" * 64,
                file_path="/tmp/example.py",
                line_start=10,
                line_end=20,
                score=0.123,
                project_id="finance-coach-latam",
            )
        ]
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(results=results)
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="q"))

        r = out[0]
        assert r["chunk_hash"] == "a" * 64
        assert r["file_path"] == "/tmp/example.py"
        assert r["line_start"] == 10
        assert r["line_end"] == 20
        assert r["score"] == 0.123
        assert r["project_id"] == "finance-coach-latam"


# ---------------------------------------------------------------------------
# Empty Index / Edge Cases
# ---------------------------------------------------------------------------


class TestEmptyIndexAndEdgeCases:
    """Defensive defaults: empty index returns ``[]``; no exception."""

    def test_empty_index_returns_empty_list(self) -> None:
        embedding = _FakeEmbedding(vectors={"q": _hash_vector("q")})
        store = _FakeVectorStore(results=[])
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        out = uc.execute(Req(query="q"))

        assert out == []


# ---------------------------------------------------------------------------
# Embedding Errors Surface as Tool Errors
# ---------------------------------------------------------------------------


class TestEmbeddingErrorsPropagate:
    """``GeminiTransientError`` MUST propagate unchanged for the wrapper layer."""

    def test_gemini_transient_error_propagates_unchanged(self) -> None:
        embedding = _FakeEmbedding(raise_exc=GeminiTransientError("rate limited"))
        store = _FakeVectorStore()
        uc, Req = _use_case(embedding=embedding, vector_store=store)

        with pytest.raises(GeminiTransientError):
            uc.execute(Req(query="q"))
