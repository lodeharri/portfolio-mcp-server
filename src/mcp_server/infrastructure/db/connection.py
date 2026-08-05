"""SQLite-vec database connection helpers.

The DB layer is a tiny adapter package: it exposes two functions and
one constant. The composition root owns the lifetime of every
connection (eager / fail-fast per ADR-001) — these helpers do not hold
any module-level state.

* :func:`open_db` — opens a sqlite connection at ``db_path`` with
  sqlite-vec loaded and ``schema.sql`` applied. Returns the connection
  in WAL mode for concurrent reads during writes.

* :func:`connect_in_memory` — convenience for unit tests that need a
  sqlite-vec-loaded ``:memory:`` database. Returns a fresh
  ``sqlite3.Connection``; the caller closes it.

* :data:`_SCHEMA_PATH` — absolute path to the ``schema.sql`` source
  file. Public re-export under :data:`SCHEMA_PATH` so tests can
  monkeypatch it for the missing-schema negative case.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Final

import sqlite_vec

from mcp_server.domain.exceptions import SchemaError

__all__ = ["SCHEMA_PATH", "connect_in_memory", "open_db"]


# ---------------------------------------------------------------------------
# Schema path resolution
# ---------------------------------------------------------------------------


def _schema_path() -> Path:
    """Return the absolute path to the bundled ``schema.sql``."""
    return Path(__file__).resolve().parent / "schema.sql"


# Public alias — tests monkeypatch this to simulate a missing schema
# file in the negative test for ``open_db``.
SCHEMA_PATH: Final[Path] = _schema_path()

# Private mirror — lets the test patch ``db.connection._SCHEMA_PATH``
# without colliding with the ``Final`` declared public one.
_SCHEMA_PATH: Final[Path] = SCHEMA_PATH


# ---------------------------------------------------------------------------
# open_db — on-disk DB
# ---------------------------------------------------------------------------


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open the ``data/index.sqlite`` database with sqlite-vec loaded.

    Steps:

    1. Ensure the parent directory exists (idempotent ``mkdir -p``).
    2. Open a sqlite3 connection.
    3. Load the ``sqlite-vec`` extension via :func:`sqlite_vec.load`.
    4. Enable WAL mode (``PRAGMA journal_mode=WAL``) so concurrent
       reads don't block writes during a preindex run.
    5. Apply ``schema.sql`` (idempotent — uses ``CREATE ... IF NOT
       EXISTS`` everywhere).

    Args:
        path: Filesystem path to the sqlite database file. Created
            if it doesn't exist; parents are ``mkdir -p``-ed.

    Returns:
        An open :class:`sqlite3.Connection`. The caller owns
        lifetime — close with ``conn.close()`` when done.

    Raises:
        SchemaError: when the bundled ``schema.sql`` cannot be read
            or its text is empty. Distinct from a runtime SQL error,
            this signals a corrupt install / sandbox setup failure
            and the CLI MUST abort with ``DB_ERROR`` (exit 5).
    """
    db_path = Path(path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        sqlite_vec.load(conn)
    except Exception as exc:  # noqa: BLE001 — surface as SchemaError
        conn.close()
        raise SchemaError(f"failed to load sqlite-vec extension: {exc}") from exc

    _enable_wal(conn)
    _apply_schema(conn)
    return conn


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Enable WAL journal mode for concurrent reads during writes."""
    cur = conn.execute("PRAGMA journal_mode=WAL")
    row = cur.fetchone()
    # Some in-memory or read-only configurations degrade WAL back to
    # ``memory`` / ``truncate``. We do not raise on those — they are
    # still safer than the default rollback journal.
    _ = row


def _read_schema_script() -> str:
    """Read ``schema.sql`` from the bundled source location.

    Wrapped in a function (instead of inline ``SCHEMA_PATH.read_text()``)
    so tests can monkeypatch it to simulate a missing/empty schema.
    """
    return SCHEMA_PATH.read_text(encoding="utf-8")


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Apply the bundled ``schema.sql`` to ``conn``.

    Runs each statement in sequence; commits at the end. The schema
    uses ``CREATE ... IF NOT EXISTS`` everywhere so the call is
    safe to repeat on an existing DB.
    """
    try:
        script = _read_schema_script()
    except (FileNotFoundError, OSError, PermissionError) as exc:
        raise SchemaError(f"schema.sql not readable at {SCHEMA_PATH}: {exc}") from exc

    if not script.strip():
        raise SchemaError(f"schema.sql at {SCHEMA_PATH} is empty")

    try:
        conn.executescript(script)
    except sqlite3.OperationalError as exc:
        raise SchemaError(f"failed to apply schema.sql: {exc}") from exc
    conn.commit()


# ---------------------------------------------------------------------------
# connect_in_memory — test helper
# ---------------------------------------------------------------------------


def connect_in_memory() -> sqlite3.Connection:
    """Open a sqlite-vec-loaded ``:memory:`` connection with the schema applied.

    Convenience for unit tests that exercise ``SqliteVecStore`` without
    touching the filesystem. Returns a fresh connection; the caller
    owns lifetime.
    """
    conn = sqlite3.connect(":memory:")
    sqlite_vec.load(conn)
    _apply_schema(conn)
    return conn
