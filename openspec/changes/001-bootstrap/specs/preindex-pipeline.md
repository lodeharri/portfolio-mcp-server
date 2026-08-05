# preindex-pipeline

## Purpose

The CLI indexing pipeline (entry point `src/mcp_server/interfaces/cli/preindex.py`) that builds `data/index.sqlite` from the manifest. It chunks manifest-declared files, hashes each chunk, calls Gemini for embeddings, runs gitleaks on each chunk, and persists both the textual and vector rows. The pipeline MUST be runnable locally with `--mock-gemini` for tests and MUST be re-runnable (idempotent via chunk-hash caching) so resume after interruption is safe.

## Schema / Interface

```python
# src/mcp_server/interfaces/cli/preindex.py
from enum import Enum

class PreindexExitCode(Enum):
    OK              = 0
    MANIFEST_ERROR  = 2
    GITLEAKS_ERROR  = 3
    GEMINI_ERROR    = 4
    DB_ERROR        = 5

def main(argv: list[str] | None = None) -> int:
    """CLI entry. Reads --manifest, --db, --mock-gemini, --quiet."""

# Domain
class CodeChunk(BaseModel):
    chunk_hash: str       # sha256 hex, 64 chars (canonical tuple includes embedding_dim)
    project_id: str
    file_path: str
    start_char: int
    end_char: int
    content: str
    embedding: list[float] # length == embedding_dim, default 768
    embedding_dim: int    # 768 today; per-dim vec table named vec_chunks_{dim}
    flagged: bool         # gitleaks FLAGGED but inserted

class ChunkHash(str):
    """sha256-hex of canonical (project_id, file_path, start_char, content, embedding_dim)."""

# Ports (declared in src/mcp_server/application/ports/)
class EmbeddingPort(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...

class SecretScannerPort(Protocol):
    def scan(self, content: str, source: str) -> ScanVerdict: ...

class VectorStorePort(Protocol):
    def upsert(self, chunks: list[CodeChunk]) -> None: ...
    def has_hash(self, chunk_hash: str) -> bool: ...

# SQLite schema (src/mcp_server/infrastructure/db/schema.sql)
# code_chunks:
#   chunk_hash  TEXT UNIQUE NOT NULL,
#   project_id  TEXT NOT NULL,
#   file_path   TEXT NOT NULL,
#   start_char  INTEGER NOT NULL,
#   end_char    INTEGER NOT NULL,
#   content     TEXT NOT NULL,
#   flagged     INTEGER NOT NULL DEFAULT 0
# vec_chunks_768 (sqlite-vec virtual table, named per embedding_dim per ADR-004):
#   chunk_hash  TEXT PRIMARY KEY,
#   embedding   float[768]
```

## Requirements

### Requirement: Pipeline Reads Manifest and Respects Scoping

The pipeline MUST read `config/projects.manifest.yaml` (or `--manifest`) and MUST refuse to walk any filesystem path that is not declared in `projects[].include_subdirs`. Directories listed in `exclude_paths` or `exclude_subdirs` MUST be skipped.

#### Scenario: Only declared projects are indexed

- GIVEN the manifest declares `finance-coach-latam` and `landing-page-portfolio`
- WHEN `preindex` runs
- THEN the audit log MUST show one `project.start` event per declared project
- AND no other directory under `/home/harri/development/projects/portfolio/` MAY be walked.

#### Scenario: Excluded subdirs are skipped

- GIVEN `finance-coach-latam.exclude_subdirs` contains `node_modules` and `.aws`
- WHEN the walker descends into that project
- THEN it MUST NOT enter `node_modules/` or `.aws/`
- AND no chunks from those directories MAY reach the embedder.

#### Scenario: File extension outside include_extensions is skipped

- GIVEN the manifest's `include_extensions` does NOT contain `.png`
- WHEN the walker encounters `docs/diagram.png`
- THEN the file MUST be ignored.

### Requirement: Chunking at 1500 / 200

The pipeline MUST split each file's text into chunks of `chunk_size=1500` characters with `chunk_overlap=200` characters. Both values MUST come from the manifest, not hardcoded.

#### Scenario: Chunk size matches manifest

- GIVEN `indexing.chunk_size=1500` and `indexing.chunk_overlap=200`
- WHEN a 2000-char file is chunked
- THEN chunks MUST be at most 1500 chars
- AND consecutive chunks MUST overlap by approximately 200 chars.

#### Scenario: Empty file is skipped

- GIVEN a file with 0 bytes or only whitespace
- WHEN the chunker processes it
- THEN no chunks SHALL be produced
- AND no Gemini call SHALL be made for that file.

### Requirement: Chunk-Hash Caching Is Idempotent

Each chunk MUST be hashed with sha256 over the canonical tuple `(project_id, file_path, start_char, content, embedding_dim)` (per ADR-004). Chunks whose hash already exists in `code_chunks` MUST be skipped entirely. The embedding_dim MUST be included in the tuple so that a future dim change does not produce silent hash collisions with old chunks.

#### Scenario: Re-run is a no-op

- GIVEN `data/index.sqlite` already contains 1000 chunks from a previous run
- WHEN `preindex` is invoked a second time with no source changes
- THEN zero new rows MUST be inserted
- AND zero Gemini requests MUST be made
- AND the audit log MUST record `cache.hits=1000`.

#### Scenario: Modified file produces a new hash

- GIVEN a file changes by one character
- WHEN its chunk is re-hashed
- THEN the new hash MUST differ from the old
- AND the new chunk MUST be inserted (the old one is left for the next change to clean up).

### Requirement: Rate-Limited Gemini Embeddings

The pipeline MUST embed chunks via the `EmbeddingPort` (Gemini 2.0 Flash + `text-embedding-004`, 768-dim) and MUST sleep at least 0.1 s between consecutive API calls to honor the free-tier RPM.

#### Scenario: 0.1 s sleep between calls

- GIVEN 5 chunks awaiting embedding
- WHEN the embedder batch is processed
- THEN consecutive calls MUST be at least 100 ms apart
- AND the total wait MUST be ≥ 0.4 s for 5 chunks.

#### Scenario: --mock-gemini skips real API

- GIVEN `--mock-gemini` is passed
- WHEN the pipeline runs
- THEN it MUST use a deterministic mock EmbeddingPort (e.g. hash of text → 768 floats)
- AND no outbound HTTP request to Google SHALL be made.

#### Scenario: Gemini rate-limit error is retried with backoff

- GIVEN the Gemini API returns HTTP 429
- WHEN the embedder handles the response
- THEN it MUST retry with exponential backoff up to 3 attempts
- AND on final failure MUST exit with `PreindexExitCode.GEMINI_ERROR`.

### Requirement: Per-Chunk Gitleaks Scan

Before a chunk is hashed, embedded, or inserted, the chunk MUST be passed through `SecretScannerPort.scan(...)`. Verdict `BLOCKED` MUST cause the chunk to be dropped; `FLAGGED` MUST cause it to be inserted with `flagged=1` and an audit log entry; `CLEAN` MUST be inserted normally.

#### Scenario: BLOCKED chunk never inserted

- GIVEN a chunk containing an AWS access key
- WHEN the chunk reaches the scanner
- THEN it MUST NOT be passed to the embedder
- AND no row MUST be inserted in `code_chunks` or `vec_chunks_768`
- AND an audit line with `event="secret.blocked"` MUST be emitted.

#### Scenario: FLAGGED chunk is inserted with flag

- GIVEN a chunk with a medium-confidence match
- WHEN the scanner returns `FLAGGED`
- THEN the chunk MUST be embedded and persisted
- AND `code_chunks.flagged` MUST equal `1`
- AND an audit line with `event="secret.flagged"` MUST be emitted.

### Requirement: Schema and Persistence

The pipeline MUST create `data/index.sqlite` containing a `code_chunks` table (with `chunk_hash TEXT UNIQUE NOT NULL` where the hash is computed over the canonical tuple `(project_id, file_path, start_char, content, embedding_dim)`) and a `vec_chunks_768` virtual table (sqlite-vec, `embedding float[768]`, named per embedding_dim per ADR-004). On startup the pipeline MUST run `schema.sql` if either table is missing. If embedding_dim changes in the future, a new vec table (`vec_chunks_{dim}`) is created and the old one is left intact (see ADR-004).

#### Scenario: Fresh DB is created

- GIVEN `data/index.sqlite` does not exist
- WHEN the pipeline starts
- THEN it MUST be created
- AND `code_chunks` MUST have `chunk_hash` as `UNIQUE NOT NULL`
- AND `vec_chunks_768` MUST be a virtual table with an `embedding` column of type `float[768]`.

#### Scenario: Resume after crash

- GIVEN the pipeline was interrupted at chunk 500 of 1000
- WHEN it is re-run
- THEN chunks 501–1000 MUST be processed
- AND chunks 1–500 MUST be skipped due to the hash cache.

## Error / Edge Cases

- Manifest reference to a missing path: preindex MUST emit `project.missing` and skip that project (do not crash the whole run).
- File read error (permission denied, binary file): MUST skip the file with `file.skipped` and continue.
- Gitleaks binary missing: MUST fail-closed (exit `GITLEAKS_ERROR`).
- `GEMINI_API_KEY` unset AND `--mock-gemini` not passed: MUST exit `GEMINI_ERROR` with a clear message.
- Disk full while writing SQLite: MUST exit `DB_ERROR` and leave the DB in a recoverable state (transaction-based writes).

## Test Scenarios

| Scenario | Required because |
|---|---|
| `is_path_indexed` is consulted for every walked file | **Layer 1** scoped indexing |
| Sanitizer is also invoked on the preindex `summary` log line (guard against secrets in paths) | **Layer 3** defense-in-depth |
| Re-running preindex is a no-op when content is unchanged | Hash cache contract |
| BLOCKED chunks never reach the embedder | **Layer 2** preindex scan |
| `--mock-gemini` produces deterministic 768-dim vectors | Testability |
| Schema contains `chunk_hash UNIQUE NOT NULL` (over canonical tuple including `embedding_dim`) and `vec_chunks_768.embedding float[768]` | Persistence contract |
