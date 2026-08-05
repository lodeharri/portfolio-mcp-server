"""Unit tests for ``src/mcp_server/domain/value_objects.py``.

The domain layer is PURE — no framework deps. The three value objects
sit alongside the entities:

* ``ChunkHash`` — ``NewType`` over ``str`` capturing the SHA-256 hex
  digest of the canonical 5-tuple ``(project_id, file_path, start_char,
  content, embedding_dim)`` per ADR-004.
* ``Vector`` — ``NewType`` over ``list[float]`` with a validator that
  enforces ``len == embedding_dim``. Use this anywhere an embedding
  needs dim validation at construction time.
* ``Embedding`` — semantic alias for ``Vector``.

The ``compute_chunk_hash`` helper is the single function that knows the
canonical tuple shape; the use case depends on it via this module.
"""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# ChunkHash
# ---------------------------------------------------------------------------


class TestChunkHashContract:
    """``ChunkHash`` is a NewType over the hex string of SHA-256."""

    def test_chunk_hash_can_be_imported(self) -> None:
        from mcp_server.domain.value_objects import ChunkHash

        assert ChunkHash is not None

    def test_compute_chunk_hash_returns_64_char_hex(self) -> None:
        from mcp_server.domain.value_objects import compute_chunk_hash

        result = compute_chunk_hash(
            project_id="p",
            file_path="/tmp/foo.py",
            start_char=0,
            content="x = 1",
            embedding_dim=768,
        )
        assert isinstance(result, str)
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_compute_chunk_hash_is_deterministic(self) -> None:
        from mcp_server.domain.value_objects import compute_chunk_hash

        a = compute_chunk_hash(
            project_id="p", file_path="/tmp/foo.py", start_char=0,
            content="x = 1", embedding_dim=768,
        )
        b = compute_chunk_hash(
            project_id="p", file_path="/tmp/foo.py", start_char=0,
            content="x = 1", embedding_dim=768,
        )
        assert a == b

    def test_compute_chunk_hash_matches_sha256_of_canonical_tuple(self) -> None:
        """Hash MUST equal sha256 of the canonical 5-tuple string.

        The canonical tuple per ADR-004 is
        ``f"{project_id}|{file_path}|{start_char}|{embedding_dim}|{content}"``.
        Verifying this guarantees the dim-change scenario (different
        embedding_dim → different hash) is enforced.
        """
        from mcp_server.domain.value_objects import compute_chunk_hash

        canonical = f"p|/tmp/foo.py|0|768|x = 1"
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        actual = compute_chunk_hash(
            project_id="p",
            file_path="/tmp/foo.py",
            start_char=0,
            content="x = 1",
            embedding_dim=768,
        )
        assert actual == expected

    def test_compute_chunk_hash_differs_when_project_id_differs(self) -> None:
        from mcp_server.domain.value_objects import compute_chunk_hash

        a = compute_chunk_hash(
            project_id="p1", file_path="/tmp/foo.py", start_char=0,
            content="x = 1", embedding_dim=768,
        )
        b = compute_chunk_hash(
            project_id="p2", file_path="/tmp/foo.py", start_char=0,
            content="x = 1", embedding_dim=768,
        )
        assert a != b

    def test_compute_chunk_hash_differs_when_file_path_differs(self) -> None:
        from mcp_server.domain.value_objects import compute_chunk_hash

        a = compute_chunk_hash(
            project_id="p", file_path="/tmp/foo.py", start_char=0,
            content="x = 1", embedding_dim=768,
        )
        b = compute_chunk_hash(
            project_id="p", file_path="/tmp/bar.py", start_char=0,
            content="x = 1", embedding_dim=768,
        )
        assert a != b

    def test_compute_chunk_hash_differs_when_start_char_differs(self) -> None:
        from mcp_server.domain.value_objects import compute_chunk_hash

        a = compute_chunk_hash(
            project_id="p", file_path="/tmp/foo.py", start_char=0,
            content="x = 1", embedding_dim=768,
        )
        b = compute_chunk_hash(
            project_id="p", file_path="/tmp/foo.py", start_char=1,
            content="x = 1", embedding_dim=768,
        )
        assert a != b

    def test_compute_chunk_hash_differs_when_content_differs(self) -> None:
        from mcp_server.domain.value_objects import compute_chunk_hash

        a = compute_chunk_hash(
            project_id="p", file_path="/tmp/foo.py", start_char=0,
            content="x = 1", embedding_dim=768,
        )
        b = compute_chunk_hash(
            project_id="p", file_path="/tmp/foo.py", start_char=0,
            content="x = 2", embedding_dim=768,
        )
        assert a != b

    def test_compute_chunk_hash_differs_when_embedding_dim_differs(self) -> None:
        """ADR-004 critical case: dim change MUST invalidate the hash."""
        from mcp_server.domain.value_objects import compute_chunk_hash

        a = compute_chunk_hash(
            project_id="p", file_path="/tmp/foo.py", start_char=0,
            content="x = 1", embedding_dim=768,
        )
        b = compute_chunk_hash(
            project_id="p", file_path="/tmp/foo.py", start_char=0,
            content="x = 1", embedding_dim=1024,
        )
        assert a != b


# ---------------------------------------------------------------------------
# Vector
# ---------------------------------------------------------------------------


class TestVectorContract:
    """``Vector`` validates the dim at construction."""

    def test_vector_can_be_imported(self) -> None:
        from mcp_server.domain.value_objects import Vector

        assert Vector is not None

    def test_vector_validates_length_matches_embedding_dim(self) -> None:
        from mcp_server.domain.value_objects import Vector

        # 768 floats → OK
        v = Vector(values=[0.0] * 768, embedding_dim=768)
        assert len(v.values) == 768
        assert v.embedding_dim == 768

    def test_vector_rejects_mismatched_length(self) -> None:
        from mcp_server.domain.value_objects import (
            EmbeddingDimensionMismatchError,
            Vector,
        )

        with pytest.raises(EmbeddingDimensionMismatchError):
            Vector(values=[0.0] * 100, embedding_dim=768)

    def test_vector_accepts_dim_1024(self) -> None:
        from mcp_server.domain.value_objects import Vector

        v = Vector(values=[0.0] * 1024, embedding_dim=1024)
        assert v.embedding_dim == 1024

    def test_vector_dict_round_trip(self) -> None:
        from mcp_server.domain.value_objects import Vector

        original = Vector(values=[0.1, 0.2, 0.3], embedding_dim=3)
        dumped = original.model_dump()
        rebuilt = Vector(**dumped)
        assert rebuilt == original


# ---------------------------------------------------------------------------
# Embedding — semantic alias
# ---------------------------------------------------------------------------


class TestEmbeddingAlias:
    """``Embedding`` is a semantic alias of ``Vector``."""

    def test_embedding_is_vector(self) -> None:
        from mcp_server.domain.value_objects import Embedding, Vector

        # Both names refer to the same class for backward compatibility.
        assert Embedding is Vector


# ---------------------------------------------------------------------------
# Sanity: value objects are pure (no I/O, no framework deps)
# ---------------------------------------------------------------------------


class TestValueObjectsPurity:
    """Domain value objects do not import infrastructure or interfaces."""

    def test_value_objects_module_does_not_import_infrastructure(self) -> None:
        # Static check: import the module and assert it never touches the
        # infrastructure layer.
        import ast
        import pathlib

        path = pathlib.Path("src/mcp_server/domain/value_objects.py")
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("mcp_server.infrastructure"), (
                        f"value_objects.py must not import infrastructure: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("mcp_server.infrastructure"), (
                    f"value_objects.py must not import infrastructure: {node.module}"
                )
