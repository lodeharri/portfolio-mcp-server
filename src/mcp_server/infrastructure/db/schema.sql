-- SQLite schema for the preindex pipeline (001-bootstrap).
--
-- This file is the single source of truth for the on-disk schema. The
-- :func:`mcp_server.infrastructure.db.connection.open_db` helper
-- loads it once per fresh database and relies on ``CREATE ... IF NOT
-- EXISTS`` so re-running is a no-op (idempotent).
--
-- Versioning strategy (ADR-004):
--
-- * The ``code_chunks`` table is dimension-agnostic — it carries the
--   ``embedding_dim`` column so a future dim change can coexist with
--   old chunks.
-- * The vector table is named per dim — ``vec_chunks_{dim}`` —
--   so a new embedding model at 1024-dim lands in
--   ``vec_chunks_1024`` without disturbing the existing 768 rows.
--
-- Caching key (per the preindex spec): ``chunk_hash`` is the SHA-256
-- hex digest of the canonical 5-tuple
-- ``(project_id, file_path, start_char, content, embedding_dim)``.
-- The ``embedding_dim`` is part of the canonical tuple so a dim
-- change produces a different hash and never silently collides with
-- old rows.

CREATE TABLE IF NOT EXISTS code_chunks (
    chunk_hash    TEXT PRIMARY KEY NOT NULL,
    project_id    TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    start_char    INTEGER NOT NULL,
    end_char      INTEGER NOT NULL,
    content       TEXT NOT NULL,
    embedding_dim INTEGER NOT NULL DEFAULT 768,
    flagged       INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_code_chunks_project ON code_chunks(project_id);
CREATE INDEX IF NOT EXISTS idx_code_chunks_file    ON code_chunks(file_path);
CREATE INDEX IF NOT EXISTS idx_code_chunks_dim     ON code_chunks(embedding_dim);

-- Virtual table for sqlite-vec. The dim is part of the table name
-- (ADR-004) so a future dim change adds a new vec_chunks_<dim> table
-- without disturbing the existing 768 rows.
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks_768 USING vec0(
    chunk_hash TEXT PRIMARY KEY,
    embedding  float[768]
);
