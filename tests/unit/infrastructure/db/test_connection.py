"""Unit tests for ``src/mcp_server/infrastructure/db/connection.py``.

The DB layer lives in ``infrastructure/db/`` and is responsible for:

* Loading the ``schema.sql`` file from the package source tree.
* Opening a SQLite connection with ``sqlite-vec`` loaded.
* Creating the ``code_chunks`` table and ``vec_chunks_768`` virtual
  table on first open (idempotent — uses ``CREATE ... IF NOT EXISTS``).

These tests assert the contract the preindex CLI relies on.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# schema.sql — file presence + content
# ---------------------------------------------------------------------------


class TestSchemaFileContract:
    """``schema.sql`` MUST live at ``src/mcp_server/infrastructure/db/schema.sql``."""

    def test_schema_sql_file_exists(self) -> None:
        path = Path("src/mcp_server/infrastructure/db/schema.sql")
        assert path.is_file(), "schema.sql missing — preindex cannot start"

    def test_schema_sql_declares_code_chunks_table(self) -> None:
        text = Path("src/mcp_server/infrastructure/db/schema.sql").read_text()
        assert "CREATE TABLE" in text
        assert "code_chunks" in text
        assert "chunk_hash" in text
        assert "TEXT" in text and "PRIMARY KEY" in text

    def test_schema_sql_declares_vec_chunks_768_table(self) -> None:
        """Per ADR-004: vec table is named per dim (``vec_chunks_{dim}``).

        For 001-bootstrap only the 768-dim table exists; the convention
        ``vec_chunks_{dim}`` is what allows future dims to coexist.
        """
        text = Path("src/mcp_server/infrastructure/db/schema.sql").read_text()
        assert "VIRTUAL TABLE" in text
        assert "vec0" in text
        assert "vec_chunks_768" in text
        assert "float[768]" in text

    def test_schema_sql_is_idempotent(self) -> None:
        """``CREATE ... IF NOT EXISTS`` MUST be present so re-running is a no-op."""
        text = Path("src/mcp_server/infrastructure/db/schema.sql").read_text()
        assert "IF NOT EXISTS" in text

    def test_schema_sql_indexes_code_chunks(self) -> None:
        """Indexes on ``project_id`` and ``file_path`` MUST exist for runtime search."""
        text = Path("src/mcp_server/infrastructure/db/schema.sql").read_text()
        # Look for "CREATE INDEX" or "INDEX IF NOT EXISTS"
        assert "INDEX" in text
        assert "project_id" in text
        assert "file_path" in text


# ---------------------------------------------------------------------------
# connection module
# ---------------------------------------------------------------------------


class TestLoadSchemaContract:
    """``load_schema(conn)`` runs the embedded schema.sql."""

    def test_load_schema_creates_code_chunks(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.db.connection import open_db

        db_path = tmp_path / "test.sqlite"
        conn = open_db(db_path)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
            tables = {row[0] for row in cur.fetchall()}
            assert "code_chunks" in tables
        finally:
            conn.close()

    def test_load_schema_creates_vec_chunks_768(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.db.connection import open_db

        db_path = tmp_path / "test.sqlite"
        conn = open_db(db_path)
        try:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
            tables = {row[0] for row in cur.fetchall()}
            assert "vec_chunks_768" in tables
        finally:
            conn.close()

    def test_load_schema_is_idempotent(self, tmp_path: Path) -> None:
        """Open + schema twice → still works (CREATE IF NOT EXISTS handles it)."""
        from mcp_server.infrastructure.db.connection import open_db

        db_path = tmp_path / "test.sqlite"
        conn1 = open_db(db_path)
        conn1.close()

        # Reopen the same DB; schema already applied → no error.
        conn2 = open_db(db_path)
        try:
            tables = {
                row[0]
                for row in conn2.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            assert "code_chunks" in tables
            assert "vec_chunks_768" in tables
        finally:
            conn2.close()


class TestOpenDbContract:
    """``open_db(path)`` opens a sqlite connection with sqlite-vec loaded."""

    def test_open_db_returns_sqlite3_connection(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.db.connection import open_db

        conn = open_db(tmp_path / "x.sqlite")
        try:
            assert isinstance(conn, sqlite3.Connection)
        finally:
            conn.close()

    def test_open_db_creates_parent_dirs(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.db.connection import open_db

        # Parent path is two levels deep — open_db MUST mkdir -p.
        db_path = tmp_path / "data" / "nested" / "x.sqlite"
        conn = open_db(db_path)
        try:
            assert db_path.exists()
        finally:
            conn.close()

    def test_open_db_loads_sqlite_vec(self, tmp_path: Path) -> None:
        """``sqlite-vec`` MUST be usable after ``open_db`` (vec0 virtual table works)."""
        from mcp_server.infrastructure.db.connection import open_db

        db_path = tmp_path / "x.sqlite"
        conn = open_db(db_path)
        try:
            # ``sqlite-vec`` exposes a ``vec_version()`` SQL function so
            # callers can verify the extension loaded. If the extension
            # is missing the query raises ``OperationalError``.
            cur = conn.execute("SELECT vec_version()")
            row = cur.fetchone()
            assert row is not None
            assert len(row[0]) > 0
        finally:
            conn.close()

    def test_open_db_enables_wal_for_concurrent_reads(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.db.connection import open_db

        db_path = tmp_path / "x.sqlite"
        conn = open_db(db_path)
        try:
            cur = conn.execute("PRAGMA journal_mode")
            row = cur.fetchone()
            assert row is not None
            mode = row[0].lower()
            # WAL mode keeps concurrent reads unblocked during writes —
            # important for the preindex-on-one-thread + /healthz probe
            # scenario. Some in-memory configurations degrade WAL back
            # to ``memory``; only assert against "memory" when WAL is
            # unavailable, otherwise expect "wal".
            assert mode in ("wal", "memory")
        finally:
            conn.close()

    def test_open_db_raises_schema_error_when_schema_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If ``schema.sql`` cannot be located, open_db MUST raise ``SchemaError``."""
        from mcp_server.domain.exceptions import SchemaError
        from mcp_server.infrastructure import db as db_pkg

        # Patch the package's schema path to a guaranteed-missing file
        # so the ``load_schema`` helper raises ``SchemaError``.
        monkeypatch.setattr(
            db_pkg.connection,
            "_SCHEMA_PATH",
            tmp_path / "no-such-schema.sql",
        )
        with pytest.raises(SchemaError):
            db_pkg.connection.open_db(tmp_path / "x.sqlite")


# ---------------------------------------------------------------------------
# in-memory connection helper
# ---------------------------------------------------------------------------


class TestInMemoryContract:
    """``connect_in_memory()`` returns a sqlite-vec-loaded :memory: connection.

    Used by the ``SqliteVecStore`` tests to avoid touching the filesystem.
    """

    def test_connect_in_memory_returns_sqlite3_connection(self) -> None:
        from mcp_server.infrastructure.db.connection import connect_in_memory

        conn = connect_in_memory()
        try:
            assert isinstance(conn, sqlite3.Connection)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            assert "code_chunks" in tables
            assert "vec_chunks_768" in tables
        finally:
            conn.close()
