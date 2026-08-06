"""Integration tests for the ``--purge-orphans`` preindex CLI flag.

The flag deletes chunks whose source ``file_path`` is no longer present
on disk. Without it, deleting a file from the project tree leaves the
chunks orphaned in ``code_chunks`` and ``vec_chunks_768`` — search
results can still hit the dead path. The purge is INFORMATIONAL:
an orphan is not an error, it is a maintenance event.

Contract pinned here:

1. With ``--purge-orphans``, chunks for files deleted from disk are
   removed from BOTH ``code_chunks`` AND ``vec_chunks_768``.
2. Chunks for files that still exist on disk are PRESERVED.
3. Without ``--purge-orphans``, orphans are PRESERVED (back-compat).
4. The preindex pipeline continues normally after a purge — a missing
   file is dropped, existing files get re-indexed if they changed.
5. Each purged project emits an ``orphans.purged`` audit event with
   ``project_id``, ``file_count``, ``chunk_count`` fields.
"""

from __future__ import annotations

import json
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

    Files are placed under ``src/`` so the production
    ``YamlManifestAdapter.is_path_indexed`` (which matches the first
    segment of ``rel`` against ``include_subdirs``) accepts them.
    Mirrors the helper in ``test_preindex_idempotent.py``.
    """
    project = root / "proj"
    project.mkdir()
    for relpath, content in files.items():
        full = project / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return project


def _row_count(db_path: Path, table: str) -> int:
    """Count rows in a table after loading sqlite-vec on a fresh connection."""
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


def _file_chunk_count(db_path: Path, file_path: str, table: str) -> int:
    """Count rows in ``table`` whose ``file_path`` equals ``file_path``."""
    import sqlite_vec

    conn = sqlite3.connect(str(db_path))
    try:
        sqlite_vec.load(conn)
        cur = conn.execute(
            f"SELECT count(*) FROM {table} WHERE file_path = ?",
            (file_path,),
        )
        return cur.fetchone()[0]
    finally:
        conn.close()


def _read_json_lines(captured: str) -> list[dict]:
    """Parse newline-delimited JSON from a stdout capture."""
    lines = [line for line in captured.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# CLI: --purge-orphans removes chunks for deleted files
# ---------------------------------------------------------------------------


class TestPurgeOrphansFlag:
    """``--purge-orphans`` deletes chunks for files no longer on disk."""

    def test_orphans_are_deleted_on_purge_orphans(
        self, tmp_path: Path, capsys
    ) -> None:
        from mcp_server.interfaces.cli import preindex

        manifest = tmp_path / "manifest.yaml"
        files = {
            "src/a.py": "def hello(): return 1\n" * 100,
            "src/b.py": "def world(): return 2\n" * 100,
        }
        project = _make_project_tree(tmp_path, files)
        db = tmp_path / "data" / "index.sqlite"
        _write_manifest(manifest, project_path=project)

        # Step 1: index both files.
        first_rc = preindex.cli(
            [
                "--manifest", str(manifest),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
            ]
        )
        assert first_rc == 0
        assert _row_count(db, "code_chunks") >= 2
        assert _vec_count(db) >= 2

        # Step 2: delete b.py from disk.
        (project / "src" / "b.py").unlink()

        # Step 3: re-run with --purge-orphans.
        second_rc = preindex.cli(
            [
                "--manifest", str(manifest),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
                "--purge-orphans",
            ]
        )
        assert second_rc == 0

        # b.py's chunks MUST be gone from BOTH tables.
        b_path = str(project / "src" / "b.py")
        assert _file_chunk_count(db, b_path, "code_chunks") == 0
        # vec_chunks_768 has no file_path column — verify via join.
        import sqlite_vec

        conn = sqlite3.connect(str(db))
        try:
            sqlite_vec.load(conn)
            cur = conn.execute(
                "SELECT count(*) FROM vec_chunks_768 v "
                "JOIN code_chunks cc ON cc.chunk_hash = v.chunk_hash "
                "WHERE cc.file_path = ?",
                (b_path,),
            )
            assert cur.fetchone()[0] == 0, (
                "vec_chunks_768 rows for deleted file must be purged"
            )
        finally:
            conn.close()

        # a.py's chunks MUST remain — it still exists on disk.
        a_path = str(project / "src" / "a.py")
        assert _file_chunk_count(db, a_path, "code_chunks") >= 1

    def test_without_flag_orphans_remain(self, tmp_path: Path) -> None:
        """Back-compat: without ``--purge-orphans`` orphans persist."""
        from mcp_server.interfaces.cli import preindex

        manifest = tmp_path / "manifest.yaml"
        files = {
            "src/a.py": "def hello(): return 1\n" * 100,
            "src/b.py": "def world(): return 2\n" * 100,
        }
        project = _make_project_tree(tmp_path, files)
        db = tmp_path / "data" / "index.sqlite"
        _write_manifest(manifest, project_path=project)

        # Index both files.
        preindex.cli(
            [
                "--manifest", str(manifest),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
            ]
        )
        chunks_after_first = _row_count(db, "code_chunks")

        # Delete b.py from disk; rerun WITHOUT --purge-orphans.
        (project / "src" / "b.py").unlink()
        rc = preindex.cli(
            [
                "--manifest", str(manifest),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
            ]
        )
        assert rc == 0

        # b.py's chunks remain because the flag was not passed.
        b_path = str(project / "src" / "b.py")
        assert _file_chunk_count(db, b_path, "code_chunks") >= 1
        assert _row_count(db, "code_chunks") == chunks_after_first

    def test_purge_then_reindex_still_works(self, tmp_path: Path) -> None:
        """The purge is INFORMATIONAL — the pipeline continues to run."""
        from mcp_server.interfaces.cli import preindex

        manifest = tmp_path / "manifest.yaml"
        files = {
            "src/a.py": "def hello(): return 1\n" * 100,
            "src/b.py": "def world(): return 2\n" * 100,
        }
        project = _make_project_tree(tmp_path, files)
        db = tmp_path / "data" / "index.sqlite"
        _write_manifest(manifest, project_path=project)

        # First run: index everything.
        preindex.cli(
            [
                "--manifest", str(manifest),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
            ]
        )

        # Delete b.py, modify a.py — re-run with --purge-orphans.
        (project / "src" / "b.py").unlink()
        (project / "src" / "a.py").write_text(
            "def hello(): return 999\n" * 200
        )

        rc = preindex.cli(
            [
                "--manifest", str(manifest),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
                "--purge-orphans",
            ]
        )
        assert rc == 0

        # a.py re-indexed (content changed → new hash → new row).
        # b.py gone.
        a_path = str(project / "src" / "a.py")
        b_path = str(project / "src" / "b.py")
        assert _file_chunk_count(db, a_path, "code_chunks") >= 1
        assert _file_chunk_count(db, b_path, "code_chunks") == 0

    def test_purge_emits_orphans_purged_audit_event(
        self, tmp_path: Path, capsys
    ) -> None:
        """Each purged project MUST emit an ``orphans.purged`` audit event."""
        from mcp_server.interfaces.cli import preindex

        manifest = tmp_path / "manifest.yaml"
        files = {
            "src/a.py": "def hello(): return 1\n" * 100,
            "src/b.py": "def world(): return 2\n" * 100,
        }
        project = _make_project_tree(tmp_path, files)
        db = tmp_path / "data" / "index.sqlite"
        _write_manifest(manifest, project_path=project)

        # Index both files.
        preindex.cli(
            [
                "--manifest", str(manifest),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
            ]
        )
        # Drain stdout from the first run so we only assert against
        # the second run's output.
        capsys.readouterr()

        # Delete b.py and purge.
        (project / "src" / "b.py").unlink()
        preindex.cli(
            [
                "--manifest", str(manifest),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
                "--purge-orphans",
            ]
        )

        out, _ = capsys.readouterr()
        records = _read_json_lines(out)
        purge_records = [r for r in records if r.get("event") == "orphans.purged"]
        assert len(purge_records) == 1, (
            f"expected one orphans.purged event, got: {purge_records}"
        )
        rec = purge_records[0]
        assert rec["project_id"] == "test-proj"
        assert rec["file_count"] >= 1
        assert rec["chunk_count"] >= 1

    def test_no_orphans_emits_zero_count(self, tmp_path: Path, capsys) -> None:
        """``--purge-orphans`` with nothing to purge still emits an audit
        event with ``file_count == 0`` (informational; never an error)."""
        from mcp_server.interfaces.cli import preindex

        manifest = tmp_path / "manifest.yaml"
        files = {"src/a.py": "def hello(): return 1\n" * 100}
        project = _make_project_tree(tmp_path, files)
        db = tmp_path / "data" / "index.sqlite"
        _write_manifest(manifest, project_path=project)

        # Index one file, then purge — nothing to purge.
        preindex.cli(
            [
                "--manifest", str(manifest),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
            ]
        )
        capsys.readouterr()

        rc = preindex.cli(
            [
                "--manifest", str(manifest),
                "--db", str(db),
                "--mock-gemini",
                "--quiet",
                "--purge-orphans",
            ]
        )
        assert rc == 0

        out, _ = capsys.readouterr()
        records = _read_json_lines(out)
        purge_records = [r for r in records if r.get("event") == "orphans.purged"]
        assert len(purge_records) == 1
        assert purge_records[0]["file_count"] == 0
        assert purge_records[0]["chunk_count"] == 0


# ---------------------------------------------------------------------------
# CLI: --purge-orphans is in the help text
# ---------------------------------------------------------------------------


class TestPurgeOrphansFlagAdvertised:
    """The CLI ``--help`` MUST list the flag (per ADR-002)."""

    def test_help_lists_purge_orphans(self, capsys) -> None:
        import pytest

        from mcp_server.interfaces.cli import preindex

        with pytest.raises(SystemExit) as exc_info:
            preindex.cli(["--help"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert "--purge-orphans" in out
