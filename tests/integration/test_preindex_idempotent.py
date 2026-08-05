"""Integration tests for ``src/mcp_server/interfaces/cli/preindex.py``.

The end-to-end contract for PR3 — run the CLI against a tiny fixture
manifest + file tree, assert the DB is created, the vec table has the
chunks, and a re-run is a no-op.

This is the operational contract that locks down ADR-004's chunk-hash
cache: a real preindex run followed by a real re-run with no source
changes MUST produce zero new embeddings and zero new DB writes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _write_manifest(
    manifest_path: Path,
    *,
    project_path: Path,
    include_subdirs: list[str] | None = None,
) -> None:
    include_subdirs = include_subdirs or ["src"]
    manifest_yaml = (
        "schema_version: 1\n"
        "server:\n"
        "  name: portfolio-mcp-server\n"
        "  version: 0.1.0\n"
        "  description: integration test\n"
        "indexing:\n"
        "  default_policy: deny\n"
        "  chunk_size: 1500\n"
        "  chunk_overlap: 200\n"
        "  include_extensions:\n"
        "    - .py\n"
        "  exclude_paths: []\n"
        "projects:\n"
        "  - id: test-proj\n"
        f"    path: {project_path}\n"
        "    display_name: Test Project\n"
        "    description: integration test\n"
        f"    include_subdirs:\n"
        f"      - {include_subdirs[0]}\n"
        "    exclude_subdirs: []\n"
    )
    manifest_path.write_text(manifest_yaml)


def _make_project_tree(root: Path, files: dict[str, str]) -> Path:
    """Create ``<root>/proj/`` and place ``files`` under it.

    The keys of ``files`` are paths RELATIVE TO the project root — so
    ``{"src/a.py": "..."}`` creates ``<root>/proj/src/a.py``. Using a
    real subdirectory (not the project root itself) is intentional: the
    production ``YamlManifestAdapter.is_path_indexed`` matches the first
    segment of ``rel`` against ``include_subdirs`` and does NOT treat
    ``"."`` as the project root. Putting files under ``src/`` keeps the
    fixture consistent with the real adapter's semantics.
    """
    project = root / "proj"
    project.mkdir()
    for relpath, content in files.items():
        full = project / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return project


def _row_count(db_path: Path, table: str) -> int:
    """Count rows in a table after loading sqlite-vec on a fresh connection.

    The ``vec_chunks_768`` table is a vec0 virtual table — without
    ``sqlite_vec.load(conn)`` the count fails with
    ``no such module: vec0``. Loading once per helper call is cheap and
    keeps the fixture self-contained.
    """
    import sqlite_vec

    conn = sqlite3.connect(str(db_path))
    try:
        sqlite_vec.load(conn)
        cur = conn.execute(f"SELECT count(*) FROM {table}")
        return cur.fetchone()[0]
    finally:
        conn.close()


def _vec_count(db_path: Path, table: str = "vec_chunks_768") -> int:
    """Count rows in a vec_chunks_<dim> table (sqlite-vec required)."""
    return _row_count(db_path, table)


# ---------------------------------------------------------------------------
# Idempotent re-run
# ---------------------------------------------------------------------------


class TestPreindexIdempotent:
    """``preindex`` re-runs are no-ops via the chunk-hash cache."""

    def test_first_run_indexes_files(self, tmp_path: Path, capsys) -> None:
        from mcp_server.interfaces.cli import preindex

        manifest = tmp_path / "manifest.yaml"
        project = _make_project_tree(
            tmp_path, {"src/a.py": "def hello(): return 1\n" * 100}
        )
        db = tmp_path / "data" / "index.sqlite"
        _write_manifest(manifest, project_path=project)

        rc = preindex.cli(
            ["--manifest", str(manifest), "--db", str(db), "--mock-gemini", "--quiet"]
        )
        assert rc == 0
        assert db.exists(), "DB file should be created"
        assert _row_count(db, "code_chunks") >= 1
        assert _vec_count(db) >= 1

    def test_second_run_is_noop(self, tmp_path: Path) -> None:
        from mcp_server.interfaces.cli import preindex

        manifest = tmp_path / "manifest.yaml"
        files = {"src/a.py": "def hello(): return 1\n" * 100}
        project = _make_project_tree(tmp_path, files)
        db = tmp_path / "data" / "index.sqlite"
        _write_manifest(manifest, project_path=project)

        # First run.
        first_rc = preindex.cli(
            ["--manifest", str(manifest), "--db", str(db), "--mock-gemini", "--quiet"]
        )
        assert first_rc == 0
        chunks_after_first = _row_count(db, "code_chunks")
        vec_after_first = _vec_count(db)
        assert chunks_after_first >= 1
        assert vec_after_first >= 1

        # Second run on unchanged source → no new rows.
        second_rc = preindex.cli(
            ["--manifest", str(manifest), "--db", str(db), "--mock-gemini", "--quiet"]
        )
        assert second_rc == 0
        assert _row_count(db, "code_chunks") == chunks_after_first
        assert _vec_count(db) == vec_after_first

    def test_db_contains_real_vectors(self, tmp_path: Path) -> None:
        from mcp_server.interfaces.cli import preindex

        manifest = tmp_path / "manifest.yaml"
        project = _make_project_tree(tmp_path, {"src/a.py": "hello world" * 100})
        db = tmp_path / "data" / "index.sqlite"
        _write_manifest(manifest, project_path=project)

        rc = preindex.cli(
            ["--manifest", str(manifest), "--db", str(db), "--mock-gemini", "--quiet"]
        )
        assert rc == 0

        # Pull a vector from the DB and verify it's 768 floats long.
        import sqlite_vec

        conn = sqlite3.connect(str(db))
        try:
            sqlite_vec.load(conn)
            cur = conn.execute(
                "SELECT length(embedding) FROM vec_chunks_768 LIMIT 1"
            )
            row = cur.fetchone()
            assert row is not None
            # Each float is 4 bytes; 768 * 4 = 3072 bytes total.
            assert row[0] == 768 * 4
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Full pipeline — exit codes + DB shape
# ---------------------------------------------------------------------------


class TestPreindexExitCodes:
    """The CLI returns the documented :class:`PreindexExitCode` values."""

    def test_ok_exit_code_on_success(self, tmp_path: Path) -> None:
        from mcp_server.interfaces.cli import preindex

        manifest = tmp_path / "manifest.yaml"
        project = _make_project_tree(tmp_path, {"src/a.py": "x = 1"})
        db = tmp_path / "data" / "index.sqlite"
        _write_manifest(manifest, project_path=project)
        rc = preindex.cli(
            ["--manifest", str(manifest), "--db", str(db), "--mock-gemini", "--quiet"]
        )
        assert rc == 0

    def test_manifest_error_on_missing_file(self, tmp_path: Path) -> None:
        from mcp_server.interfaces.cli import preindex

        db = tmp_path / "index.sqlite"
        rc = preindex.cli(
            [
                "--manifest", str(tmp_path / "missing.yaml"),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
            ]
        )
        assert rc == 2  # MANIFEST_ERROR
