# Design: 001-bootstrap — Foundation, Security Layers, Preindex Pipeline

## Technical Approach

Hexagonal Python package `src/mcp_server/` with **eager composition root** at `create_app()`. All adapters wire at startup; use cases receive ports via constructor. The 5-layer security model lives in `src/mcp_server/security/` and is crossed twice: at write-time (preindex → gitleaks) and at read-time (every MCP/HTTP response → OutputSanitizer). Index is baked into the runtime image by the Dockerfile builder, so the running container is read-only and warm on cold start. CLI entry `preindex` runs under `python -m mcp_server.interfaces.cli.preindex` (with `--mock-gemini` for tests) and reuses the same composition graph for testability.

## Architecture Overview

```
                ┌────────────────────────────────────────────────────────┐
                │              src/mcp_server/  (hexagonal)               │
                │                                                         │
   interfaces/  │  ┌─────────┐  ┌──────────────┐  ┌─────────────────────┐  │
   ┌──────────► │  │ http/   │  │ mcp/ (later) │  │ cli/preindex.py     │  │
   │            │  └────┬────┘  └──────┬───────┘  └─────────┬───────────┘  │
   │            │       │              │                    │              │
   │            │  ┌────▼──────────────▼────────────────────▼──────────┐  │
   │            │  │            application/use_cases/                │  │
   │            │  │  (depend only on domain + ports)                 │  │
   │            │  └──────────┬──────────────────────┬───────────────┘  │
   │            │             │                      │                  │
   │            │  ┌──────────▼──────┐   ┌────────────▼───────────────┐  │
   │            │  │ domain/ (PURE)  │   │ application/ports/         │  │
   │            │  │  entities,      │   │  Protocols: Embedding, LLM,│  │
   │            │  │  value objects, │   │  VectorStore, Secret,      │  │
   │            │  │  exceptions     │   │  Manifest, RateLimiter     │  │
   │            │  └─────────────────┘   └────────────┬───────────────┘  │
   │            │                                     │                  │
   │            │           composition.py (ONLY wiring point)           │
   │            │                                     │                  │
   │            │  ┌──────────────────────────────────▼────────────────┐ │
   │            │  │            infrastructure/adapters/               │ │
   │            │  │  gemini_embedding · sqlite_vec_store · gemini_llm │ │
   │            │  │  gitleaks_scanner · yaml_manifest · slowapi       │ │
   │            │  └───────────────────────────────────────────────────┘ │
   │            │                                                         │
   │            │  security/  (cross-cutting, registered as a middleware │
   │            │              AND injected into the preindex use case)  │
   │            └─────────────────────────────────────────────────────────┘
```

`create_app()` calls `composition.compose()` exactly once. `composition.compose()` returns a frozen `Container` dataclass holding concrete adapter instances + use cases. Adapters and use cases are constructed eagerly — `gemini_embedding.GeminiAdapter` builds its `genai.Client` at startup, the slowapi limiter instantiates its in-memory backend, and `sqlite_vec_store.SqliteVecStore` opens `data/index.sqlite` (or opens a read-only handle if the file is missing). Web framework / domain never imports the other.

## Composition Root Wiring Strategy — Eager

Picked eager (not lazy) wiring. See `adrs/001-composition-eager-vs-lazy.md`. Single `compose()` call at `create_app()`, returning a `Container` whose use cases hold already-built adapter references. Rationale (TL;DR): (a) simpler — the 256 MB Fly machine has plenty of headroom for one `genai.Client` + one SQLite handle; (b) fail-fast — adapter init errors surface during `/healthz` instead of during the first user query; (c) the spec for `app-bootstrap` Scenario "Composition root is the only wiring point" becomes trivially testable with a static import-graph assertion.

```python
# src/mcp_server/composition.py (signature only)
@dataclass(frozen=True)
class Container:
    config: AppConfig
    manifest_port: ManifestPort
    embedding_port: EmbeddingPort
    scanner_port: SecretScannerPort
    vector_port: VectorStorePort
    rate_limiter: RateLimiterPort
    audit: AuditLogger
    sanitizer: OutputSanitizer
    preindex_use_case: PreindexUseCase
    search_use_case: SearchCodeUseCase      # wired but not exercised in 001
    list_projects_use_case: ListProjectsUseCase

def compose(config: AppConfig | None = None) -> Container: ...
```

## Capability 1 — `app-bootstrap`

**`src/mcp_server/app.py`** exposes `create_app() -> FastAPI` and `run()`. `create_app()`:
1. Calls `load_config()` once; falls back to env-default `AppConfig` if `config=` is passed (test override).
2. Calls `composition.compose(config)` exactly once; assertion in a test verifies single-call.
3. Registers `/healthz` route returning `{status, version, commit_sha, built_at}`.
4. Mounts `FastMCP` sub-app at `/mcp` (FastMCP ≥ 3.2.4 supports `mount(app, path)`).
5. Registers the OutputSanitizer as a `BaseHTTPMiddleware` that runs **after** the route handler so it inspects the final bytes.
6. Registers slowapi `Limiter` with key `get_remote_address` and a default `30/minute` policy.

**`src/mcp_server/config.py`** is the only module that calls `os.environ`. Pydantic v2 `BaseModel` `AppConfig` with `port: int`, `log_level: str`, `manifest_path: str`, `gemini_api_key: SecretStr | None`, `embedding_dim: Literal[768, 1024]` (default 768), plus derived `db_path: str`.

**`src/mcp_server/build_info.py`** reads `COMMIT_SHA`/`BUILT_AT` once at import time, defaulting to `"unknown"`. Version is read from `importlib.metadata.version("mcp-server-playground")`.

## Capability 2 — `security-layers`

Five modules under `src/mcp_server/security/`. Each module exposes **one Protocol-conforming class** plus a `get_default()` factory (composition calls the factory).

| Module | Type | Implements | Notes |
|---|---|---|---|
| `manifest_loader.py` | dataclass | `ManifestPort` | Pure Pydantic; reads YAML; `is_path_indexed` is default-deny |
| `gitleaks_scanner.py` | class | `SecretScannerPort` | `subprocess.run(["gitleaks", "detect", "--no-git", "--source", tmpdir])`; exit → `ScanVerdict` mapping |
| `output_sanitizer.py` | class | `OutputSanitizer` | One `re.sub` per pattern; threadsafe (compiled regexes are module-level) |
| `rate_limiter.py` | class | `RateLimiterPort` | Wraps `slowapi.Limiter`; adapter delegates `check(key)` |
| `audit.py` | class | `AuditLogger` | `structlog` JSON logger writing to stdout (Docker picks up) |

`OutputSanitizer` registers both as middleware (HTTP responses) and as a callable used inside use cases (preindex summary line, MCP tool return values). One implementation, two integration points.

## Capability 3 — `preindex-pipeline`

`src/mcp_server/interfaces/cli/preindex.py` is the CLI entry. `python -m mcp_server.interfaces.cli.preindex` runs `main(argv)` which:

1. Parses args (`--manifest PATH` default from `AppConfig`, `--db PATH`, `--mock-gemini`, `--chunk-size`, `--chunk-overlap` — manifest wins unless flag overrides).
2. Calls `composition.compose(config)` — same composition root as the web app.
3. Calls `container.preindex_use_case.run(projects)`.
4. Exits with `PreindexExitCode`.

`PreindexUseCase` (in `application/use_cases/index_project.py`):
```
for project in manifest.projects:
    for file in walk(project.path, include_subdirs, exclude_subdirs, include_extensions):
        if not manifest.is_path_indexed(file): continue   # Layer 1 double-check
        text = read(file)
        if not text.strip(): continue
        for chunk_text, start, end in chunk(text, 1500, 200):
            chunk_hash = sha256(f"{project_id}|{file}|{start}|{chunk_text}")
            if vector_port.has_hash(chunk_hash):
                cache_hits += 1; continue                  # idempotent
            verdict = scanner_port.scan(chunk_text, file)   # Layer 2
            if verdict == BLOCKED:
                audit.warn("secret.blocked", source=file); continue
            flagged = (verdict == FLAGGED)
            if flagged: audit.warn("secret.flagged", source=file)
            vector = embedding_port.embed_one(chunk_text)   # respects 0.1s sleep
            vector_port.upsert([CodeChunk(...)])            # chunk_hash UNIQUE
```

`ChunkHash = NewType` over `str`; `CodeChunk` is a frozen Pydantic model. Mock embedding adapter implements `EmbeddingPort` with `hashlib.sha256(text).digest() → 768 floats in [-1, 1]` for `--mock-gemini`. CLI contract details: `adrs/002-preindex-cli-contract.md`. Retry policy on real Gemini: `adrs/003-gemini-retry-policy.md`.

## Capability 4 — `container-image`

Multi-stage Dockerfile. Builder stage installs `build-essential`, `gcc`, the package, and gitleaks (`GITLEAKS_VERSION=8.18.4` tarball from GitHub release). Preindex runs **with `--mock-gemini` fallback** if `GEMINI_API_KEY` secret is unset (spec scenario "Build without GEMINI_API_KEY still succeeds"). Runtime stage copies only `/opt/venv`, `/app/data/index.sqlite`, and `/app/src`. USER `mcp` (UID 10001), `HEALTHCHECK` via inline `httpx.get`, `CMD ["uvicorn", "mcp_server.app:app", "--host", "0.0.0.0", "--port", "${PORT}", "--workers", "1"]` — `$PORT` is an `ENV` defaulted to `8080`, overridable per-deploy.

`docker history` and `docker run env` checks are baked into the verify phase. Size budget: `--no-cache-dir` on every `pip install`, no `build-essential` in runtime, single Python base image. Final size target: ~120 MB (room under the 150 MB ceiling).

## Data Flow — MCP Request Lifecycle

```
client (MCP) ──POST /mcp──► FastAPI ──► /mcp sub-app (FastMCP)
                                            │
                                            ▼
                              ┌─────────────────────────────┐
                              │ FastMCP tool dispatcher     │
                              │  e.g. search_harrison_code() │
                              └──────────────┬──────────────┘
                                             │
                  ┌──────────────────────────▼───────────────────────┐
                  │ application/use_cases/search_code.py            │
                  │  SearchCodeUseCase.execute(query, language?)     │
                  │  - ratelimit.check(ip)                           │
                  │  - embed_query → vector                           │
                  │  - vector_port.search(top_k=10)                   │
                  │  - manifest.filter(projects)                      │
                  └──────────────┬───────────────────────────────────┘
                                 │
                ┌────────────────▼────────────────┐
                │ adapters:                       │
                │  EmbeddingPort.embed_one()      │
                │  VectorStorePort.search()       │
                │  ManifestPort.filter()          │
                └────────────────┬────────────────┘
                                 │
                                 ▼  raw SearchResult rows
                  ┌──────────────────────────────────────┐
                  │ OutputSanitizer.sanitize(payload)   │  ← Layer 3
                  │  re.sub for AWS/GH/OpenAI/Gemini/.. │
                  └──────────────┬───────────────────────┘
                                 │
                                 ▼ redacted payload
                  audit.info("tool.response", tool=..., redactions=N)
                                 │
                                 ▼
                  JSON response → FastMCP → client (MCP)
```

## Data Flow — Secret-Redaction Flow

```
  chunk text  ──►  SecretScannerPort.scan(content, source)
                          │   subprocess: gitleaks detect --no-git --source <tmp>
                          │   exit 0 → CLEAN
                          │   exit 1 → BLOCKED   (high confidence)
                          │   exit 2 → FLAGGED   (medium, future gitleaks)
                          ▼
              ┌───────────────────────────────────────────────────┐
              │   audit.warn("secret.blocked"|"secret.flagged",   │
              │             source=..., pattern=...)              │
              └───────────────┬───────────────────────────────────┘
                              │
                              ▼
            BLOCKED: skip (no embed, no insert)
            FLAGGED: insert with flagged=1
            CLEAN:   insert

  tool response  ──►  OutputSanitizer.sanitize(body)
                              │   re.sub per SecretPattern → [REDACTED]
                              │   returns RedactionResult(text, incidents)
                              ▼
              ┌───────────────────────────────────────────────────┐
              │   audit.warn("response.redacted",                  │
              │             source=<tool|route>,                   │
              │             pattern=..., count=N)                  │
              └───────────────┬───────────────────────────────────┘
                              │
                              ▼
                  redacted body  →  wire
```

## File Changes

| File | Action | Description |
|---|---|---|
| `pyproject.toml` | modify | Add `preindex` console_script; add `structlog`, `tenacity` (or hand-rolled retry); add `gitleaks` Python wrapper optional-dep |
| `Dockerfile` | modify | Multi-stage rewrite per capability 4; gitleaks tarball in builder; BuildKit `--secret`; `--mock-gemini` fallback |
| `fly.toml` | modify | Document `PORT` env forwarding; no structural change |
| `.dockerignore` | create | Exclude `tests/`, `.git/`, `playground/`, `data/`, `openspec/` from build context |
| `src/mcp_server/app.py` | create | `create_app()`, `run()`; mount FastMCP at `/mcp`; register `/healthz`, middleware |
| `src/mcp_server/config.py` | create | `AppConfig` Pydantic v2; `load_config()` |
| `src/mcp_server/build_info.py` | create | `BuildInfo` dataclass; reads env once |
| `src/mcp_server/composition.py` | create | `Container`, `compose()` — single wiring point |
| `src/mcp_server/domain/entities.py` | create | `CodeChunk`, `Project`, `SearchResult` frozen Pydantic models |
| `src/mcp_server/domain/value_objects.py` | create | `ChunkHash`, `Vector` (768-float), `ScanVerdict` |
| `src/mcp_server/domain/exceptions.py` | create | Domain errors (no framework deps) |
| `src/mcp_server/application/ports/*.py` | create | `EmbeddingPort`, `LLMPort`, `VectorStorePort`, `SecretScannerPort`, `ManifestPort`, `RateLimiterPort` Protocols |
| `src/mcp_server/application/use_cases/index_project.py` | create | `PreindexUseCase` |
| `src/mcp_server/application/use_cases/list_projects.py` | create | Used by MCP tool later; placeholder now |
| `src/mcp_server/application/use_cases/search_code.py` | create | Placeholder; wired through composition for 001 |
| `src/mcp_server/infrastructure/adapters/gemini_embedding.py` | create | `GeminiEmbeddingAdapter` with retry/backoff (ADR-003) |
| `src/mcp_server/infrastructure/adapters/gemini_llm.py` | create | Skeleton for 002 |
| `src/mcp_server/infrastructure/adapters/sqlite_vec_store.py` | create | `SqliteVecStore` over sqlite-vec virtual table |
| `src/mcp_server/infrastructure/adapters/gitleaks_scanner.py` | create | `GitleaksScanner` subprocess wrapper |
| `src/mcp_server/infrastructure/adapters/yaml_manifest.py` | create | `YamlManifestAdapter` |
| `src/mcp_server/infrastructure/adapters/slowapi_rate_limiter.py` | create | `SlowapiRateLimiter` |
| `src/mcp_server/infrastructure/db/schema.sql` | create | `code_chunks`, `vec_chunks` virtual table |
| `src/mcp_server/infrastructure/db/connection.py` | create | SQLite connect helper; WAL mode |
| `src/mcp_server/security/manifest_loader.py` | create | Pydantic manifest model + loader (mirror of `yaml_manifest`) |
| `src/mcp_server/security/gitleaks_scanner.py` | create | Public re-export / thin wrapper (Layer 2 of the 5 layers) |
| `src/mcp_server/security/output_sanitizer.py` | create | Regex sanitizer; `SecretPattern` enum |
| `src/mcp_server/security/rate_limiter.py` | create | Wraps slowapi limiter |
| `src/mcp_server/security/audit.py` | create | structlog JSON logger |
| `src/mcp_server/interfaces/cli/preindex.py` | create | CLI entry; argparse; `main(argv)` |
| `src/mcp_server/interfaces/http/healthz.py` | create | `/healthz` handler |
| `src/mcp_server/interfaces/mcp/server.py` | create | FastMCP sub-app factory |
| `tests/unit/**` | create | mirror `src/mcp_server/`; RED-first per task |
| `tests/integration/` | create | `test_healthz.py`, `test_preindex_idempotent.py`, `test_sanitizer_middleware.py` |
| `tests/e2e/playground/` | empty for 001 | only `conftest.py` + smoke shell |

## Interfaces / Contracts

```python
# application/ports/embedding.py
class EmbeddingPort(Protocol):
    def embed_one(self, text: str) -> list[float]: ...
    def embed_many(self, texts: list[str]) -> list[list[float]]: ...

# application/ports/secret_scanner.py
class SecretScannerPort(Protocol):
    def scan(self, content: str, source: str) -> ScanVerdict: ...

# application/ports/vector_store.py
class VectorStorePort(Protocol):
    def upsert(self, chunks: list[CodeChunk]) -> None: ...
    def search(self, query_vec: list[float], top_k: int) -> list[SearchResult]: ...
    def has_hash(self, chunk_hash: str) -> bool: ...

# application/ports/manifest.py
class ManifestPort(Protocol):
    def load(self, path: str) -> Manifest: ...
    def is_path_indexed(self, path: str) -> bool: ...
    def projects(self) -> list[ProjectEntry]: ...
```

All ports are `Protocol` (structural typing) — no inheritance required, easier mocking.

## Testing Strategy

| Layer | What | Approach |
|---|---|---|
| Unit (domain) | `CodeChunk`, `Vector`, `ChunkHash`, `ScanVerdict` | Pure assertions; no I/O |
| Unit (application) | `PreindexUseCase` with all 6 ports mocked | Given/When/Then per scenario; pytest fixtures swap mock adapters per test |
| Unit (infrastructure) | Each adapter independently | subprocess mocked for gitleaks; `sqlite-vec` in-memory for vec store; slowapi in-memory limiter; structlog captured via `capture_logs` |
| Unit (security) | `OutputSanitizer` table-driven; `ManifestLoader` schema-fixture round-trip; `AuditLogger` JSON-shape assertion | parametrized `pytest.mark.parametrize` |
| Integration | `/healthz` 200 + sanitizer applied to body; `create_app()` is idempotent; composition graph static-checked | `httpx.AsyncClient` against `create_app()` |
| Integration | preindex end-to-end with `--mock-gemini` on a tmp manifest | writes to `tmp_path/data/index.sqlite`; asserts cache hit on re-run |
| E2E | Smoke: container boots, `/healthz` returns 200, image size asserted | Playwright is parked for `003-playground-ui` |
| Static | Import-graph test asserting `composition.py` is the only module importing both `infrastructure/adapters/` and `application/use_cases/` | AST walk in `tests/integration/test_hexagonal_invariants.py` |

Coverage target 60% lines on `src/mcp_server/` (matches `pyproject.toml` `fail_under = 60`). Apply phase uses `--strict-markers --strict-config` and gates on the 60% floor.

## Performance & Memory (256 MB Fly machine)

- `genai.Client` is a thin HTTP wrapper; init footprint ~5 MB. Acceptable.
- `sqlite-vec` virtual table on SQLite — single file, mmap-friendly. Keep DB file in `/app/data/index.sqlite`; never on tmpfs (sqlite-vec needs mmap).
- slowapi limiter — in-memory dict, ~negligible. Stays in-process because `--workers 1`.
- structlog — single JSON writer; ~1 MB.
- **Total runtime footprint estimate**: ~95 MB (Python + deps + sqlite-vec) + ~5 MB (adapters) + index size. Comfortable under 256 MB.
- `EXPOSE $PORT` default 8080; uvicorn single worker; concurrency limited in `fly.toml` to 50/25 (connections).
- Preindex is **build-time only** — no runtime cost. It runs in the builder stage, not in the deployed container.

## Threat Matrix

Boundary relevance: this change introduces **subprocess execution** (gitleaks) and **secret-bearing JSON to stdout** (audit log). Matrix per `references/threat-matrix.md`:

| Boundary | Applicability | Reason | Design response | RED test |
|---|---|---|---|---|
| Subprocess exec (gitleaks) | **Applicable** | New boundary; untrusted content passed via tmpdir | Run with `cwd=/tmp/scan-<uuid>`, `check=False`, parse stdout JSON, never `shell=True`; pass content via stdin not argv (avoids argv-injection of `;` `&`); `GITLEAKS_BINARY` path from validated env var, not user input | `test_gitleaks_subprocess_injection_safe` — content with `"; rm -rf #` does not exec shell |
| Path traversal in `is_path_indexed` | **Applicable** | Manifest is YAML, paths come from disk | Normalize via `pathlib.Path.resolve()`; reject if `resolve()` raises or escapes manifest root | `test_manifest_loader_blocks_traversal` — `../../../etc/passwd` returns `False` |
| Secret log leak via audit JSON | **Applicable** | `audit.warn(source=...)` may echo user path containing a token | Sanitize the `source=` field through `OutputSanitizer` before serializing | `test_audit_sanitizes_source_field` — fake audit call with token-shaped path emits `[REDACTED]` in JSON |
| Git/PR automation | **N/A** | No git commands run in production code | — | — |
| Executable file classification | **N/A** | No `.sh`/`.exe` execution paths | — | — |

## Migration / Rollout

Greenfield; no migration. The first apply creates the index from the manifest. Subsequent rebuilds are no-ops thanks to chunk-hash caching. To force a full rebuild: `rm data/index.sqlite && python -m mcp_server.interfaces.cli.preindex`.

## Open Questions

Resolved in this design — see `adrs/`:

- ✅ **Q1** Composition style → eager (`adrs/001-composition-eager-vs-lazy.md`)
- ✅ **Q2** `--mock-gemini` CLI contract → `adrs/002-preindex-cli-contract.md`
- ✅ **Q3** Gemini retry policy → `adrs/003-gemini-retry-policy.md`
- ✅ **Q4** Embedding dim versioning → `adrs/004-embedding-dim-versioning.md`

No blockers. Ready for `sdd-tasks`.

## ADRs

- [`001-composition-eager-vs-lazy.md`](adrs/001-composition-eager-vs-lazy.md)
- [`002-preindex-cli-contract.md`](adrs/002-preindex-cli-contract.md)
- [`003-gemini-retry-policy.md`](adrs/003-gemini-retry-policy.md)
- [`004-embedding-dim-versioning.md`](adrs/004-embedding-dim-versioning.md)