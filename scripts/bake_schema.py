"""Bake a schema-only ``data/index.sqlite`` for the Docker image.

The image build context does NOT include the sibling project trees
referenced by ``config/projects.manifest.yaml`` (absolute paths
outside the build context). So we cannot run the full ``preindex``
pipeline at build time — it would have no source code to embed.

Instead, we create a schema-only DB that the runtime server can boot
from. The runtime will populate it with real embeddings at runtime
(via a mounted volume on Fly.io, a persistent disk on Hugging Face
Spaces, etc.) when the user runs ``preindex`` against the actual
project trees.

This is per the spec scenario "Build without GEMINI_API_KEY still
succeeds" — the runtime serves ``/healthz`` with 200 even before
``preindex`` populates the index.

The schema is identical to what ``preindex`` would create on first
run (per ``src/mcp_server/infrastructure/db/schema.sql``). The
sqlite-vec extension is loaded via ``sqlite_vec.load(conn)`` so the
``vec_chunks_768`` virtual table is queryable.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import sqlite_vec


def main() -> int:
    """Create the schema-only DB at the path given by argv[1]."""
    if len(sys.argv) != 2:
        print("usage: bake_schema.py <db_path>", file=sys.stderr)
        return 4  # PreindexExitCode.DB_ERROR

    db_path = Path(sys.argv[1])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_path = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "mcp_server"
        / "infrastructure"
        / "db"
        / "schema.sql"
    )
    schema_sql = schema_path.read_text(encoding="utf-8")

    conn = sqlite3.connect(str(db_path))
    try:
        sqlite_vec.load(conn)
        conn.executescript(schema_sql)
        conn.commit()
    finally:
        conn.close()

    print(f"Baked schema-only index.sqlite at {db_path} ({db_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
