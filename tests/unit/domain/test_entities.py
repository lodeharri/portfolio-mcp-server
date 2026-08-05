"""Unit tests for ``src/mcp_server/domain/entities.py``.

The domain layer is PURE — no framework deps. Three Pydantic v2 frozen
models live here per the preindex-pipeline spec:

* ``CodeChunk`` — a single embedded text segment with optional
  embedding metadata.
* ``Project`` — the application-layer view of a manifest project entry.
* ``SearchResult`` — a vector-search hit returned by the runtime.

These tests assert the contracts that the preindex use case and
SQLite adapter rely on.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# CodeChunk
# ---------------------------------------------------------------------------


class TestCodeChunkContract:
    """``CodeChunk`` is the canonical chunk-of-indexed-source-text unit."""

    def test_code_chunk_can_be_imported(self) -> None:
        from mcp_server.domain.entities import CodeChunk

        assert CodeChunk is not None

    def test_code_chunk_minimal_construction(self) -> None:
        from mcp_server.domain.entities import CodeChunk

        chunk = CodeChunk(
            chunk_hash="a" * 64,
            project_id="finance-coach-latam",
            file_path="/tmp/proj/backend/auth.py",
            start_char=0,
            end_char=100,
            content="def hello(): ...",
            embedding=[0.1] * 768,
        )
        assert chunk.project_id == "finance-coach-latam"
        assert chunk.file_path == "/tmp/proj/backend/auth.py"
        assert chunk.start_char == 0
        assert chunk.end_char == 100
        assert chunk.content == "def hello(): ..."
        assert len(chunk.embedding) == 768
        assert chunk.embedding_dim == 768  # default
        assert chunk.flagged is False  # default

    def test_code_chunk_default_embedding_dim_is_768(self) -> None:
        from mcp_server.domain.entities import CodeChunk

        chunk = CodeChunk(
            chunk_hash="a" * 64,
            project_id="p",
            file_path="/tmp/foo.py",
            start_char=0,
            end_char=10,
            content="x = 1",
            embedding=[0.0] * 768,
        )
        assert chunk.embedding_dim == 768

    def test_code_chunk_default_flagged_is_false(self) -> None:
        from mcp_server.domain.entities import CodeChunk

        chunk = CodeChunk(
            chunk_hash="a" * 64,
            project_id="p",
            file_path="/tmp/foo.py",
            start_char=0,
            end_char=10,
            content="x = 1",
            embedding=[0.0] * 768,
        )
        assert chunk.flagged is False

    def test_code_chunk_explicit_embedding_dim_is_respected(self) -> None:
        from mcp_server.domain.entities import CodeChunk

        chunk = CodeChunk(
            chunk_hash="a" * 64,
            project_id="p",
            file_path="/tmp/foo.py",
            start_char=0,
            end_char=10,
            content="x = 1",
            embedding=[0.0] * 768,
            embedding_dim=768,
        )
        assert chunk.embedding_dim == 768

    def test_code_chunk_flagged_can_be_true(self) -> None:
        from mcp_server.domain.entities import CodeChunk

        chunk = CodeChunk(
            chunk_hash="a" * 64,
            project_id="p",
            file_path="/tmp/foo.py",
            start_char=0,
            end_char=10,
            content="x = 1",
            embedding=[0.0] * 768,
            flagged=True,
        )
        assert chunk.flagged is True

    def test_code_chunk_is_frozen(self) -> None:
        from mcp_server.domain.entities import CodeChunk

        chunk = CodeChunk(
            chunk_hash="a" * 64,
            project_id="p",
            file_path="/tmp/foo.py",
            start_char=0,
            end_char=10,
            content="x = 1",
            embedding=[0.0] * 768,
        )
        with pytest.raises((ValidationError, dataclasses.FrozenInstanceError, AttributeError, TypeError)):
            chunk.flagged = True  # type: ignore[misc]

    def test_code_chunk_chunk_hash_validation_accepts_64_hex(self) -> None:
        from mcp_server.domain.entities import CodeChunk

        valid_hash = hashlib.sha256(b"hello").hexdigest()
        chunk = CodeChunk(
            chunk_hash=valid_hash,
            project_id="p",
            file_path="/tmp/foo.py",
            start_char=0,
            end_char=10,
            content="x = 1",
            embedding=[0.0] * 768,
        )
        assert len(chunk.chunk_hash) == 64


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------


class TestProjectContract:
    """``Project`` is the canonical project entry shape."""

    def test_project_can_be_imported(self) -> None:
        from mcp_server.domain.entities import Project

        assert Project is not None

    def test_project_minimal_construction(self) -> None:
        from mcp_server.domain.entities import Project

        project = Project(
            id="finance-coach-latam",
            path=Path("/tmp/proj"),
            display_name="Finance Coach LATAM",
            description="Personal finance assistant",
            include_subdirs=["backend"],
            exclude_subdirs=["node_modules"],
        )
        assert project.id == "finance-coach-latam"
        assert project.path == Path("/tmp/proj")
        assert project.display_name == "Finance Coach LATAM"
        assert project.description == "Personal finance assistant"
        assert project.include_subdirs == ["backend"]
        assert project.exclude_subdirs == ["node_modules"]

    def test_project_round_trips_through_model_dump(self) -> None:
        from mcp_server.domain.entities import Project

        project = Project(
            id="p",
            path=Path("/tmp/x"),
            display_name="X",
            description="",
            include_subdirs=["src"],
            exclude_subdirs=[],
        )
        dumped = project.model_dump()
        rebuilt = Project(**dumped)
        assert rebuilt == project


# ---------------------------------------------------------------------------
# SearchResult
# ---------------------------------------------------------------------------


class TestSearchResultContract:
    """``SearchResult`` is a single vector-search hit."""

    def test_search_result_can_be_imported(self) -> None:
        from mcp_server.domain.entities import SearchResult

        assert SearchResult is not None

    def test_search_result_minimal_construction(self) -> None:
        from mcp_server.domain.entities import SearchResult

        result = SearchResult(
            chunk_hash="a" * 64,
            file_path="/tmp/proj/backend/auth.py",
            line_start=10,
            line_end=20,
            content="def hello(): ...",
            score=0.92,
            project_id="finance-coach-latam",
        )
        assert result.chunk_hash == "a" * 64
        assert result.file_path == "/tmp/proj/backend/auth.py"
        assert result.line_start == 10
        assert result.line_end == 20
        assert result.content == "def hello(): ..."
        assert result.score == 0.92
        assert result.project_id == "finance-coach-latam"

    def test_search_result_round_trips_through_model_dump(self) -> None:
        from mcp_server.domain.entities import SearchResult

        result = SearchResult(
            chunk_hash="b" * 64,
            file_path="/tmp/x.py",
            line_start=1,
            line_end=5,
            content="...",
            score=0.5,
            project_id="p",
        )
        dumped = result.model_dump()
        rebuilt = SearchResult(**dumped)
        assert rebuilt == result


# ---------------------------------------------------------------------------
# Re-exports — domain/entities.py re-exports Project at app port level
# ---------------------------------------------------------------------------


class TestProjectReexportThroughAppPort:
    """The application port still exposes ``Project`` (back-compat for PR2)."""

    def test_project_reexported_at_app_port(self) -> None:
        from mcp_server.application.ports.manifest import Project as PortProject
        from mcp_server.domain.entities import Project as DomainProject

        assert PortProject is DomainProject
