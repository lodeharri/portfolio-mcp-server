# ADR 004: Embedding dim versioning strategy

- **Status**: Accepted
- **Date**: 2026-08-05
- **Change**: `001-bootstrap`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

The sqlite-vec virtual table is currently declared with a hardcoded dimension:

```sql
CREATE VIRTUAL TABLE vec_chunks USING vec0(
  chunk_hash TEXT PRIMARY KEY,
  embedding float[768]
);
```

If Google ships a new embedding model with a different output dimension (Gemini already offers 768 and 1024 variants; future models could be 1536 or 2048), every existing chunk must be re-embedded. The runtime query path encodes the expected dimension into the cosine-distance call:

```sql
SELECT chunk_hash, distance
FROM vec_chunks
WHERE embedding MATCH ?  -- bind the 768-float query vector here
ORDER BY distance
LIMIT 10;
```

If a 1024-float query is bound against a 768-dim table, sqlite-vec raises `RuntimeError: incompatible vector dimensions`. The bug is loud — but the data is corrupted from the agent's perspective.

The proposal does not mention dim versioning; the spec hardcodes 768. We must decide NOW how the schema will accommodate a future dim change so we don't have to write a destructive migration in 2027.

## Decision Drivers

- **D1**: Future-proof for dim changes without a destructive migration.
- **D2**: Backward compatible — existing 768-dim indexes work unchanged.
- **D3**: Testable — the schema supports both 768 and 1024 (and any future N) under the same code path.
- **D4**: No real cost on the 256 MB Fly machine — sqlite-vec is single-file, no external service.
- **D5**: Spec compliance — spec says `float[768]` TODAY. We don't break the spec; we lay the groundwork for change.

## Considered Options

### Option A — Store dim alongside the embedding; one vec table per dim (chosen)

**Schema v2:**

```sql
CREATE TABLE code_chunks (
  chunk_hash   TEXT UNIQUE NOT NULL,
  project_id   TEXT NOT NULL,
  file_path    TEXT NOT NULL,
  start_char   INTEGER NOT NULL,
  end_char     INTEGER NOT NULL,
  content      TEXT NOT NULL,
  embedding_dim INTEGER NOT NULL DEFAULT 768,
  flagged      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (chunk_hash, embedding_dim)
);

CREATE VIRTUAL TABLE vec_chunks_768 USING vec0(
  chunk_hash TEXT PRIMARY KEY,
  embedding float[768]
);

-- Optional: only created if a 1024-dim embedding ever lands
-- CREATE VIRTUAL TABLE vec_chunks_1024 USING vec0(
--   chunk_hash TEXT PRIMARY KEY,
--   embedding float[1024]
-- );
```

`SqliteVecStore.upsert()` routes to the correct vec table by `embedding_dim`. `SqliteVecStore.search(query_vec, top_k)` reads `len(query_vec)`, picks the right table, runs the MATCH.

**Pros**:
- Live dim changes (e.g. mixed 768 + 1024 chunks during a transition) just work.
- No destructive migration; add a new table for the new dim, leave the old one read-only.
- One schema covers any future N.

**Cons**:
- More SQL boilerplate (one `CREATE VIRTUAL TABLE` per supported dim).
- Search code branches on dim (but it's a small `match` statement).

### Option B — Store dim in `code_chunks`, rebuild on change (rejected)

Single vec table. On dim change: full rebuild from `code_chunks` (which keeps content). Requires downtime and write amplification.

**Pros**: simpler schema today. **Cons**: rebuild = full re-embed against Gemini = costs quota and time. The whole point of chunk-hash caching is to avoid re-embedding. Option B defeats that.

### Option C — Document as future work, ship 768-only (rejected)

Spec-compliance literal interpretation. Punt to "later change" if/when Gemini changes.

**Pros**: smallest change for 001. **Cons**: leaves a known sharp edge for a future Harri to step on. Not aligned with the spec's emphasis on hexagonal flexibility.

### Option D — Pre-allocate separate tables and decide at runtime (chosen variant of A)

Same as Option A but always pre-create `vec_chunks_768` and `vec_chunks_1024` (and document the convention: "add a new `vec_chunks_{dim}` for every new dim you support"). Keeps the schema migrations small.

## Decision

**Option A (with Option D's "always pre-create known dims" flavor)** — but for 001-bootstrap we **only create `vec_chunks_768`** because that's the only dim we use today. The `embedding_dim` column in `code_chunks` records what we stored.

```sql
-- src/mcp_server/infrastructure/db/schema.sql (001-bootstrap)
CREATE TABLE IF NOT EXISTS code_chunks (
  chunk_hash   TEXT UNIQUE NOT NULL,
  project_id   TEXT NOT NULL,
  file_path    TEXT NOT NULL,
  start_char   INTEGER NOT NULL,
  end_char     INTEGER NOT NULL,
  content      TEXT NOT NULL,
  embedding_dim INTEGER NOT NULL DEFAULT 768,
  flagged      INTEGER NOT NULL DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks_768 USING vec0(
  chunk_hash TEXT PRIMARY KEY,
  embedding float[768]
);
```

### Migration path for a future dim change (e.g. 768 → 1024)

1. Bump `AppConfig.embedding_dim` env to `1024`.
2. Add `CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks_1024 USING vec0(...)` to `schema.sql` (idempotent).
3. Preindex re-runs; new embeddings land in `vec_chunks_1024` because they have a new `chunk_hash` (hash includes content, content unchanged, but the dim column changes → actually the chunk_hash stays the same! see below).

**Wait** — chunk_hash canonical tuple is `(project_id, file_path, start_char, content)` — it does NOT include `embedding_dim`. A re-embedding at a new dim would produce the SAME hash and be skipped. **Bug.**

### Fix: include `embedding_dim` in the canonical hash tuple

Update `ChunkHash` spec:

```python
chunk_hash = sha256(f"{project_id}|{file_path}|{start_char}|{embedding_dim}|{content}")
```

Spec scenario "Modified file produces a new hash" still passes. New scenario: "Re-embedding at a new dim produces new hashes" — added to the preindex spec as a follow-up for sdd-tasks.

The schema gains `embedding_dim` in the canonical tuple; `has_hash(chunk_hash)` correctly distinguishes 768 chunks from 1024 chunks even when content is identical. Old `vec_chunks_768` rows are kept (read-only) so the runtime can serve stale results until the user triggers a rebuild.

### Query path

```python
# VectorStorePort.search(query_vec, top_k)
dim = len(query_vec)
table = f"vec_chunks_{dim}"
sql = f"SELECT chunk_hash, distance FROM {table} WHERE embedding MATCH ? ORDER BY distance LIMIT ?"
```

`sqlite-vec` requires the dimension of the bind vector to match the table. We never bind the wrong dim.

### `embedding_dim` validation

`AppConfig.embedding_dim: int` defaults to 768; `EMBEDDING_DIM` env override. On preindex startup, validate that the configured dim matches what the embedder will produce. If the embedder is `GeminiEmbeddingAdapter(model="text-embedding-004")`, it produces 768 floats. Cross-check at startup; fail loudly if mismatch.

## Consequences

**Positive**:
- Adding a 1024-dim model later is a single-line `CREATE VIRTUAL TABLE` plus an env flag flip.
- Mixed-dim corpus (during a transition) is supported.
- Runtime query path stays simple: read `len(query_vec)`, pick the table.
- No destructive migration; old data stays queryable.

**Negative**:
- Schema is slightly more complex than the spec's literal `float[768]`. Documented in design.md and `schema.sql`.
- Chunk-hash canonical tuple changes → must update existing tests.
- One extra column (`embedding_dim INTEGER`) and one extra per-dim virtual table.

**Compliance with rules**:
- Spec scenario "Schema contains `chunk_hash UNIQUE NOT NULL` and `vec_chunks.embedding float[768]`" — the table is renamed to `vec_chunks_768`; semantics preserved (the 768-dim chunks live in it). Acceptable spec evolution; tracked in `sdd-archive` as a spec delta.
- `invariants` → "All chunks pass through gitleaks-python before being inserted into the vector index" — unchanged.

## Follow-ups

- Update the `preindex-pipeline` spec to add the scenario "Re-embedding at a new dim produces new hashes".
- In a future change (post-`002-mcp-tools`), add a `vec_chunks_1024` virtual table when/if we need it.
- Add a CLI flag `preindex --rebuild-dim 1024` (out of scope for 001) for explicit dim migration.