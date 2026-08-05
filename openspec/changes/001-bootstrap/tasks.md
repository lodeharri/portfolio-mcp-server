# Tasks: 001-bootstrap — Foundation, Security Layers, Preindex Pipeline

> Sequential, single-session-capable. Each task names a concrete file/change. Tests
> are written first (RED), implementation second (GREEN), refactor third (REFACTOR).
> Order respects hexagonal dependency direction: domain → ports → use cases →
> adapters → interfaces → composition → app → container. Pre-commit + CI
> `secret-scan` + `lint` + `test` MUST pass before any task is marked `[x]`.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~3,500–4,500 (35 new `src/` + 25 new `tests/` + Dockerfile + pyproject + config) |
| 400-line budget risk | **High** |
| Chained PRs recommended | **Yes** |
| Suggested split | PR1 foundations+factory → PR2 security-layers → PR3 preindex-pipeline → PR4 container-image |
| Delivery strategy | `ask-on-risk` (config: `prs: ask-always`) |
| Chain strategy | `stacked-to-main` |

```text
Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High
```

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Hexagonal invariants + app factory + `/healthz` + FastMCP mount | PR1 | `pytest tests/integration/test_hexagonal_invariants.py tests/integration/test_healthz.py -q` | `uvicorn mcp_server.app:app --port 8080` + `curl /healthz` | Revert src/mcp_server/{app.py,config.py,composition.py,build_info.py,interfaces/http/*,interfaces/mcp/*} |
| 2 | Five security adapters wired through composition (manifest, gitleaks, sanitizer, rate-limit, audit) | PR2 | `pytest tests/unit/security tests/integration/test_sanitizer_middleware.py -q` | CLI: `python -m mcp_server.interfaces.cli.preindex --mock-gemini --limit-files 1` | Revert src/mcp_server/security/* + adapter wiring in composition.py |
| 3 | Preindex pipeline (domain, ports, use case, CLI, mock-Gemini, schema) | PR3 | `pytest tests/unit/domain tests/unit/application/use_cases tests/integration/test_preindex_idempotent.py -q` | `python -m mcp_server.interfaces.cli.preindex --mock-gemini` end-to-end | Revert src/mcp_server/{domain,application,interfaces/cli, infrastructure/db, infrastructure/adapters/gemini_embedding, infrastructure/adapters/sqlite_vec_store} |
| 4 | Multi-stage Dockerfile, non-root runtime, healthcheck, size gate, `docker build` smoke | PR4 | `pytest -q` (full unit+integration) | `docker build -t mcp-server:test . && docker run --rm mcp-server:test python -c "import httpx; ..."` | Revert Dockerfile + `.dockerignore`; runtime image reverts to previous tag |

> **Work-unit isolation note**: PR2 must NOT call `preindex_use_case.run()`; it only wires
> security adapters. PR3 introduces the preindex use case and CLI. This prevents PR2 from
> pulling domain entities forward and keeps each PR diff under the 400-line review budget.

---

## Phase 0 — Hexagonal Invariants (PR1, first gate)

- [x] 0.1 **RED — write `tests/integration/test_hexagonal_invariants.py`** that walks `src/mcp_server/` with `ast` and asserts:
  - `src/mcp_server/domain/**` imports nothing from `application/`, `infrastructure/`, `interfaces/`, or `security/`
  - `src/mcp_server/application/use_cases/**` imports nothing from `infrastructure/` or `interfaces/`
  - `src/mcp_server/interfaces/**` imports nothing from `infrastructure/`
  - `src/mcp_server/composition.py` is the **only** module that imports from both `infrastructure/adapters/` and `application/use_cases/`
  - Run `pytest tests/integration/test_hexagonal_invariants.py -q` — must FAIL (current tree has no code to enforce the rule).
- [x] 0.2 **GREEN — install `pytest` test framework**: add `pytest>=8.3`, `pytest-asyncio>=0.24`, `pytest-cov>=5.0`, `httpx>=0.27`, `ruff>=0.7` to `pyproject.toml` `[project.optional-dependencies] dev`; add `[tool.coverage]` and `[tool.pytest.ini_options]` blocks (markers `unit`, `integration`, `security`, `e2e`); verify `pip install -e ".[dev]"` and `pytest --collect-only` work. (Runner currently `declared-not-installed` per `openspec/config.yaml`; flips to `installed` after this task.)
- [x] 0.3 **REFACTOR — extend invariant test** to also enforce the layered rule in `tests/integration/test_hexagonal_invariants.py`: `composition.py` is the only module importing `infrastructure.adapters.*` AND `application.use_cases.*` simultaneously. Add a docstring naming the ADR-001 reference.

## Phase 1 — app-bootstrap (PR1)

- [x] 1.1 **RED — `tests/unit/test_config.py`**: parametrize cases for `load_config()` reading `PORT`, `LOG_LEVEL`, `MANIFEST_PATH`, `GEMINI_API_KEY`, `EMBEDDING_DIM`, `COMMIT_SHA`, `BUILT_AT`. Assert `ValidationError` on `PORT=abc` and default fallbacks.
- [x] 1.2 **GREEN — `src/mcp_server/config.py`**: define `AppConfig` (Pydantic v2 `BaseSettings`-style with explicit `os.environ` access) with `port: int = 8080`, `log_level: str = "INFO"`, `manifest_path: str = "config/projects.manifest.yaml"`, `gemini_api_key: SecretStr | None = None`, `embedding_dim: int = 768`, `commit_sha: str = "unknown"`, `built_at: str = "unknown"`, derived `db_path: str = "data/index.sqlite"`. `load_config() -> AppConfig` is the only function calling `os.environ`. Build-time metadata (`commit_sha`, `built_at`, `embedding_dim`) lives in `AppConfig`, not in a separate `build_info.py` module (the latter would re-introduce an env-reading module and break the single-source-of-env rule).
- [x] 1.3 **RED — `tests/unit/test_composition.py`**: assert `compose(test_config)` returns a frozen `Container`; call `compose()` twice → two distinct container instances (no module-level cache).
- [x] 1.4 **GREEN — `src/mcp_server/composition.py`**: `@dataclass(frozen=True) class Container` with placeholder fields (config, manifest_port, embedding_port, scanner_port, vector_port, rate_limiter, audit, sanitizer, preindex_use_case, search_use_case, list_projects_use_case) and `compose(config: AppConfig | None = None) -> Container` that builds nothing yet but returns the dataclass. Re-raises on missing manifest path with a clear message.
- [x] 1.5 **RED — `tests/integration/test_healthz.py`**: `httpx.AsyncClient(create_app())` → `GET /healthz` returns 200 with JSON containing `status, version, commit_sha, built_at`. With `COMMIT_SHA`/`BUILT_AT` unset, both equal `"unknown"`.
- [x] 1.6 **GREEN — `src/mcp_server/interfaces/http/healthz.py`**: `GET /healthz` returning `{status: "ok", version, commit_sha, built_at}` sourced from `AppConfig` and `importlib.metadata.version("mcp-server-playground")`.
- [x] 1.7 **RED — `tests/integration/test_app_factory.py`**: `create_app()` returns a `FastAPI` whose `app.title == "mcp-server-playground"`; `compose()` is called exactly once per factory invocation; `create_app()` is idempotent.
- [x] 1.8 **GREEN — `src/mcp_server/app.py`**: `create_app(config: AppConfig | None = None) -> FastAPI` — calls `load_config()` if `config` is `None`, calls `composition.compose(config)` exactly once, registers `OutputSanitizerMiddleware` (no-op stub in PR1, real impl in PR2), and `adds /healthz` route. `run()` reads `AppConfig.port` and `os.environ.get("PORT")` is forbidden here — port must come from `load_config()`.
- [x] 1.9 **RED — `tests/integration/test_mcp_mount.py`**: with FastMCP server created and `mcp_app = mcp.http_app(path="/")`, mounting on FastAPI at `/mcp` results in `GET /mcp` returning the MCP transport response (use a `httpx.AsyncClient` with a minimal `initialize` payload). Mounts must NOT match `/healthz` or any other path.
- [x] 1.10 **GREEN — `src/mcp_server/interfaces/mcp/server.py`**: build a `FastMCP("mcp-server-playground")` instance, return `mcp.http_app(path="/")` for the ASGI sub-app. Document the FastMCP 3.2.4+ pattern: `FastAPI(lifespan=mcp_app.lifespan)` then `app.mount("/mcp", mcp_app)`. `create_app()` wires this in. The repo stays tool-less in PR1 (the `mcp.tool` registrations come in `002-mcp-tools`).
- [x] 1.11 **REFACTOR — add `pyproject.toml` entry**: under `[project.scripts]`, ensure `mcp-server = "mcp_server.app:run"` exists (already present per current `pyproject.toml` — verify and leave untouched). Update `pyproject.toml` `[project.optional-dependencies] dev` only if Phase 0.2 missed anything.

## Phase 2 — security-layers (PR2)

> **Scope discipline**: PR2 wires security adapters and registers them in `composition.py`.
> It does NOT exercise `preindex_use_case.run()` and does NOT touch the CLI yet
> (PR3). This keeps the PR2 diff within the 400-line budget.

- [ ] 2.1 **RED — `tests/unit/security/test_manifest_loader.py`**: parametrize (`declared → True`, `excluded subdir → False`, `unrelated path → False`, `../ traversal → False`, `invalid schema → ManifestSchemaError`, `missing file → ManifestNotFoundError`). Mock the YAML fixture via `tmp_path`.
- [ ] 2.2 **GREEN — `src/mcp_server/security/manifest_loader.py`**: `YamlManifestAdapter` reads YAML, validates against the Pydantic `Manifest` model (schema_version, server, indexing, projects), `is_path_indexed(path)` is default-deny (only `True` for paths inside a declared project's `include_subdirs` and not in `exclude_subdirs`). Raise `ManifestSchemaError`, `ManifestNotFoundError`, `ManifestPermissionError` as per spec.
- [ ] 2.3 **RED — `tests/unit/security/test_gitleaks_scanner.py`**: parametrize (`exit 0 → CLEAN`, `exit 1 → BLOCKED`, `exit 2 → FLAGGED`, `missing binary → GitleaksBinaryMissingError`, `malformed JSON → BLOCKED` (fail-closed)). Mock `subprocess.run` via `unittest.mock`; assert invocation passes content via `tmp_path` (not argv), `shell=False`, `check=False`.
- [ ] 2.4 **GREEN — `src/mcp_server/security/gitleaks_scanner.py`**: `GitleaksScanner` calls `subprocess.run(["gitleaks", "detect", "--no-git", "--source", tmpdir], capture_output=True, text=True, check=False, shell=False, cwd=tmpdir)`; maps exit code to `ScanVerdict`. `GitleaksBinaryMissingError` raised on `FileNotFoundError`. Threat-matrix row "Subprocess exec": content passed via stdin/file not argv, so `;` and `&` cannot break out.
- [ ] 2.5 **RED — `tests/unit/security/test_output_sanitizer.py`**: parametrize table-driven — (`AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE`, `ghp_`+36 word chars, `sk-`+48 chars, `AIza`+35 chars, `api_key=abc123`, `secret: hunter2`, `clean text` → no change, `incidents` empty).
- [ ] 2.6 **GREEN — `src/mcp_server/security/output_sanitizer.py`**: `OutputSanitizer` with module-level compiled regexes for `SecretPattern` (AWS, GITHUB, OPENAI, GEMINI, GENERIC). `sanitize(text, source) -> RedactionResult` returns redacted text and `list[RedactionIncident]`. Threadsafe (no shared mutable state).
- [ ] 2.7 **RED — `tests/unit/security/test_audit.py`**: capture stdout via `capsys`, call `audit.warn("secret.blocked", source=..., pattern=...)` and assert the emitted line is valid JSON containing `event, level, timestamp, source, pattern`. Threat-matrix row "Secret log leak via audit JSON": call with token-shaped source → source field MUST appear as `[REDACTED]` in JSON.
- [ ] 2.8 **GREEN — `src/mcp_server/security/audit.py`**: `AuditLogger` backed by `structlog` configured with `JSONRenderer`, writing to stdout. The `source=` field flows through `OutputSanitizer.sanitize(text, source=...)` before serialization.
- [ ] 2.9 **RED — `tests/unit/security/test_rate_limiter.py`**: with a fresh in-memory `slowapi.Limiter`, 30 calls in 60s → all return `True`; 31st call → returns `False` (test via the `check(key)` adapter method, not the HTTP route).
- [ ] 2.10 **GREEN — `src/mcp_server/security/rate_limiter.py`**: `SlowapiRateLimiter` wraps `slowapi.Limiter(key_func=get_remote_address, default_limits=["30/minute"])`; exposes `check(key: str) -> bool` per the `RateLimiterPort` Protocol (Protocol added in 2.11).
- [ ] 2.11 **GREEN — `src/mcp_server/application/ports/*.py`** (six files, one Protocol each): `embedding.py`, `vector_store.py`, `llm.py`, `secret_scanner.py`, `manifest.py`, `rate_limiter.py`. Each Protocol matches the `specs/security-layers.md` and `specs/preindex-pipeline.md` schema (e.g., `ManifestPort.load(path)`, `is_path_indexed(path)`, `projects()`; `SecretScannerPort.scan(content, source) -> ScanVerdict`; `RateLimiterPort.check(key) -> bool`). No concrete classes here.
- [ ] 2.12 **RED — `tests/integration/test_sanitizer_middleware.py`**: with `OutputSanitizerMiddleware` registered and a route returning a body containing `ghp_abc...`, the response body MUST contain `[REDACTED]` and the audit log MUST contain `event="response.redacted"`.
- [ ] 2.13 **GREEN — `src/mcp_server/interfaces/http/middleware/sanitizer.py`**: `BaseHTTPMiddleware` that calls `OutputSanitizer.sanitize(body, source=route)` and rewrites the response. Register in `create_app()` (replace the PR1 stub).
- [ ] 2.14 **GREEN — `src/mcp_server/composition.py`**: extend `Container` to hold `manifest_port`, `scanner_port`, `sanitizer`, `rate_limiter`, `audit`. `compose()` wires the real adapters: `YamlManifestAdapter(config.manifest_path)`, `GitleaksScanner()`, `OutputSanitizer()`, `SlowapiRateLimiter()`, `AuditLogger()`. Fail-fast on missing manifest. `preindex_use_case`/`search_use_case`/`list_projects_use_case` stay as `None` placeholders until PR3.
- [ ] 2.15 **REFACTOR — re-run invariant test**: `tests/integration/test_hexagonal_invariants.py` MUST still pass (PR2 does not introduce new illegal imports). `pytest --cov=src/mcp_server` should hold ≥60% lines on the security modules.

## Phase 3 — preindex-pipeline (PR3)

> **Scope discipline**: PR3 adds the CLI, the use case, the schema, the SQLite
> adapter, the mock-Gemini adapter, and the chunker. It depends on PR1 (config,
> composition) and PR2 (manifest, scanner, sanitizer, audit). It does NOT touch
> the Dockerfile (PR4).

- [ ] 3.1 **RED — `tests/unit/domain/test_entities.py`**: `CodeChunk.frozen` (assign-after-construct raises), `chunk_hash` is 64-char hex, `embedding_dim` defaults to 768, `flagged` default False. `SearchResult` and `Project` round-trip through model_dump.
- [ ] 3.2 **GREEN — `src/mcp_server/domain/entities.py`**: `CodeChunk`, `Project`, `SearchResult` as frozen Pydantic v2 models per `specs/preindex-pipeline.md`.
- [ ] 3.3 **RED — `tests/unit/domain/test_value_objects.py`**: `ChunkHash` is a `NewType` over `str`; `compute_chunk_hash(project_id, file_path, start_char, content, embedding_dim)` is sha256 of `f"{project_id}|{file_path}|{start_char}|{embedding_dim}|{content}"`; same inputs → same 64-char hex; differing `embedding_dim` → different hash (ADR-004 critical case). `Vector` is `list[float]` length 768 (or any dim).
- [ ] 3.4 **GREEN — `src/mcp_server/domain/value_objects.py`**: `ChunkHash`, `Vector`, `ScanVerdict` (already declared in `value_objects.py` per design.md; verify and re-export here). `compute_chunk_hash` is the canonical 5-tuple hash.
- [ ] 3.5 **GREEN — `src/mcp_server/domain/exceptions.py`**: `DomainError` base + `ManifestSchemaError`, `ManifestNotFoundError`, `ManifestPermissionError`, `GitleaksBinaryMissingError`, `GeminiTransientError`, `GeminiPermanentError`, `PreindexAbortedError`. No framework imports.
- [ ] 3.6 **GREEN — `src/mcp_server/infrastructure/db/schema.sql`**: `CREATE TABLE IF NOT EXISTS code_chunks (chunk_hash TEXT UNIQUE NOT NULL, project_id TEXT NOT NULL, file_path TEXT NOT NULL, start_char INTEGER NOT NULL, end_char INTEGER NOT NULL, content TEXT NOT NULL, embedding_dim INTEGER NOT NULL DEFAULT 768, flagged INTEGER NOT NULL DEFAULT 0)` + `CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks_768 USING vec0(chunk_hash TEXT PRIMARY KEY, embedding float[768])`. Idempotent (`IF NOT EXISTS`).
- [ ] 3.7 **RED — `tests/unit/infrastructure/db/test_connection.py`**: open a `tmp_path/index.sqlite`, apply `schema.sql`, assert `code_chunks` and `vec_chunks_768` exist; reopening with `isolation_level=None` and `PRAGMA journal_mode=WAL` succeeds.
- [ ] 3.8 **GREEN — `src/mcp_server/infrastructure/db/connection.py`**: `open_db(path) -> sqlite3.Connection` enables WAL, `synchronous=NORMAL`, `foreign_keys=ON`, applies `schema.sql` if tables missing.
- [ ] 3.9 **RED — `tests/unit/infrastructure/adapters/test_gemini_embedding.py`**: with `MockTransport` returning 200 + JSON embedding once, return list of 768 floats. With 429 → 200 sequence, the second call succeeds and `time.sleep` was called within `[0, 1.0]` (full jitter lower bound). With 400 response → raises `GeminiPermanentError` without sleeping. With 3× 429 → raises `GeminiTransientError`.
- [ ] 3.10 **GREEN — `src/mcp_server/infrastructure/adapters/gemini_embedding.py`**: `GeminiEmbeddingAdapter(api_key, model="models/text-embedding-004")` with hand-rolled retry per ADR-003 (3 attempts, base_delay 1.0s, max_delay 30.0s, full jitter, retry on 429/500/502/503/504 + connect/timeout, fail-fast on 400/401/403/404). Two error types: `GeminiTransientError`, `GeminiPermanentError`. **0.1s sleep between successful calls lives in `PreindexUseCase`, NOT here** (separation of concerns per ADR-003 follow-up).
- [ ] 3.11 **RED — `tests/unit/infrastructure/adapters/test_sqlite_vec_store.py`**: `upsert([CodeChunk])` writes both `code_chunks` row and `vec_chunks_768` row keyed by `chunk_hash`. `has_hash(hash)` returns True after upsert, False before. `search(query_vec, top_k=10)` returns rows ordered by `distance`. A 1024-float query against the 768 table raises `RuntimeError` (sqlite-vec contract; documented).
- [ ] 3.12 **GREEN — `src/mcp_server/infrastructure/adapters/sqlite_vec_store.py`**: `SqliteVecStore` opens via `connection.open_db`, applies schema, implements `VectorStorePort`. `search` routes by `len(query_vec)` to `vec_chunks_{dim}` (only 768 exists today per ADR-004). Includes `read_only_missing_ok=True` mode for PR1 `/healthz` graceful boot when the file is missing.
- [ ] 3.13 **RED — `tests/unit/infrastructure/adapters/test_yaml_manifest.py`**: thin re-export test confirming `YamlManifestAdapter` satisfies `ManifestPort` Protocol and `is_path_indexed` returns the same results as `security.manifest_loader.YamlManifestAdapter` (one impl, two Protocol views per design.md).
- [ ] 3.14 **GREEN — `src/mcp_server/infrastructure/adapters/yaml_manifest.py`**: `YamlManifestAdapter` re-exports the security-layer implementation to satisfy the `ManifestPort` Protocol from the application layer. (One class, two Protocol views — keeps "one adapter per port" without duplication.)
- [ ] 3.15 **RED — `tests/unit/application/use_cases/test_index_project.py`**: with all six ports mocked, given a manifest with 1 project and 2 files, then:
  - happy path: 2 files → 3 chunks → all embedded → all upserted → 0 cache_hits
  - BLOCKED chunk → 0 embed calls, 0 upsert, audit emits `secret.blocked`
  - FLAGGED chunk → 1 upsert with `flagged=True`, audit emits `secret.flagged`
  - second run (same DB) → cache_hits == 3, 0 new embeddings
  - empty file → 0 chunks
  - excluded subdir not walked (assert via `walk` mock)
  - 0.1s sleep between consecutive `embed` calls (assert via `monkeypatch.setattr(time, "sleep")` call count)
- [ ] 3.16 **GREEN — `src/mcp_server/application/use_cases/index_project.py`**: `PreindexUseCase` orchestrates per design.md `## Capability 3` pseudo-code. Sleeps 0.1s between successful embeddings. Honors cache via `vector_port.has_hash(hash)` BEFORE calling the embedder. Catches `GeminiTransientError` after 3 retries and re-raises as `PreindexAbortedError(GEMINI_ERROR)`.
- [ ] 3.17 **GREEN — `src/mcp_server/application/use_cases/list_projects.py`**: `ListProjectsUseCase.execute() -> list[ProjectEntry]` (placeholder body returning `manifest_port.projects()`). Wired in PR1; fleshed out here so the MCP tool in `002-mcp-tools` is trivial.
- [ ] 3.18 **GREEN — `src/mcp_server/application/use_cases/search_code.py`**: placeholder `SearchCodeUseCase` raising `NotImplementedError`; wired in composition to keep the import-graph invariant happy, body is for `002-mcp-tools`.
- [ ] 3.19 **RED — `tests/unit/interfaces/cli/test_preindex.py`**: parametrize flags (`--manifest`, `--db`, `--mock-gemini`, `--quiet`, `--chunk-size`, `--chunk-overlap`, `--limit-files`, `--help`). Assert exit codes via `PreindexExitCode` (0 OK, 2 MANIFEST_ERROR, 3 GITLEAKS_ERROR, 4 GEMINI_ERROR, 5 DB_ERROR). Auto-`--mock-gemini` fallback when `GEMINI_API_KEY` unset and `--mock-gemini` not passed.
- [ ] 3.20 **GREEN — `src/mcp_server/interfaces/cli/preindex.py`**: argparse with the full flag surface per ADR-002; `cli(argv) -> int` calls `main(argv)`. Both `main` (return int) and `cli` (no return) wrappers exist so `pyproject.toml` console_script can point at either. End-of-run summary on stdout as JSON.
- [ ] 3.21 **REFACTOR — `pyproject.toml`**: add `preindex = "mcp_server.interfaces.cli.preindex:cli"` to `[project.scripts]` so both `preindex ...` and `python -m mcp_server.interfaces.cli.preindex` work.
- [ ] 3.22 **GREEN — `src/mcp_server/composition.py`**: complete the `Container` wiring for PR3 — `embedding_port` (`GeminiEmbeddingAdapter` real or `MockEmbeddingAdapter` per `--mock-gemini`), `vector_port` (`SqliteVecStore`), `preindex_use_case` (real `PreindexUseCase`).
- [ ] 3.23 **REFACTOR — integration smoke**: `tests/integration/test_preindex_idempotent.py` — run `python -m mcp_server.interfaces.cli.preindex --mock-gemini --db <tmp>/index.sqlite --manifest <tmp>/manifest.yaml` against a tmp tree with one `.py` file, assert the DB exists, `vec_chunks_768` has 1+ rows, re-run is a no-op, exit code is 0.

## Phase 4 — container-image (PR4)

> **Scope discipline**: PR4 touches only the Dockerfile, `.dockerignore`, the
> `deploy.yml` workflow, and the `verify` size gate. Source code is unchanged.
> Build-time secret handling and the runtime port expansion are the two
> high-risk items.

- [ ] 4.1 **RED — `tests/integration/test_docker_size.py`** (skip with `pytest.importorskip("docker")` and a `CI` env marker): build the image via `docker build -t mcp-server:test .` and assert `docker image ls mcp-server:test --format '{{.Size}}'` reports a size < 150 MB. This test is opt-in in CI; locally it requires Docker.
- [ ] 4.2 **GREEN — `.dockerignore`** (new file): exclude `.git/`, `tests/`, `playground/`, `data/`, `openspec/changes/`, `*.sqlite`, `coverage.xml`, `.pytest_cache/`, `.ruff_cache/`, `.atl/`, `.engram/`, `__pycache__/`. Reduces build context size.
- [ ] 4.3 **GREEN — `Dockerfile` builder stage rewrite**:
  - `ARG GITLEAKS_VERSION=8.18.4`; download the Go tarball into `/usr/local/bin/gitleaks`; clean up the tarball after install.
  - `ARG BAKE_INDEX=on` (BuildKit `--build-arg`); only run preindex when `BAKE_INDEX=on`.
  - `RUN --mount=type=secret,id=gemini GEMINI_API_KEY=$(cat /run/secrets/gemini) python -m mcp_server.interfaces.cli.preindex || echo "WARN: preindex skipped (no API key)"`. Note: the baked index will be empty unless the build context includes the manifest and the sibling project trees; the current manifest uses absolute paths outside the build context. PR4 does NOT make `docker build` produce a non-empty baked index — that is documented as a follow-up (Phase 4 risk #1). The container must still boot and `/healthz` returns 200.
  - `--mount=type=cache,target=/root/.cache/pip` on `pip install` to keep layer size down.
- [ ] 4.4 **GREEN — `Dockerfile` runtime stage rewrite**:
  - `python:3.10.12-slim` base (already present).
  - `groupadd --system --gid 10001 mcp && useradd --system --uid 10001 --gid mcp --create-home mcp`.
  - `WORKDIR /app`; `COPY --chown=mcp:mcp --from=builder /opt/venv /opt/venv`; `COPY --chown=mcp:mcp src ./src`; `COPY --chown=mcp:mcp config ./config`; `COPY --chown=mcp:mcp pyproject.toml README.md ./`.
  - `COPY --chown=mcp:mcp --from=builder /app/data/index.sqlite ./data/index.sqlite` (best-effort; ok if missing).
  - `ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8080`.
  - `EXPOSE ${PORT}` — Note: `EXPOSE` only accepts literals; use `EXPOSE 8080` in the Dockerfile and document the per-deploy override in `fly.toml` (which already pins `PORT = "8080"`).
  - `USER mcp`.
  - `HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD ["python", "-c", "import os, httpx, sys; sys.exit(0 if httpx.get(f'http://localhost:{os.environ.get(\"PORT\", \"8080\")}/healthz', timeout=4).status_code==200 else 1)"]` — uses Python's `os.environ.get` because JSON-form `CMD` does NOT expand shell variables; this matches the existing `Dockerfile` pattern.
  - `CMD` is **shell form** (not JSON) so `$PORT` expands at runtime: `CMD uvicorn mcp_server.app:app --host 0.0.0.0 --port ${PORT} --workers 1`. Document the per-platform port behavior (Fly 8080, HF Spaces 7860, Render 10000) in the spec scenario.
- [ ] 4.5 **GREEN — `Dockerfile` secret-leak guard**: a CI step in `deploy.yml` runs `docker history mcp-server:test --no-trunc` and `docker run --rm mcp-server:test env | grep -i gemini` to assert the key is not present. If `GEMINI_API_KEY` is not in the build env, this is a no-op (test passes vacuously).
- [ ] 4.6 **GREEN — `.github/workflows/deploy.yml`**: add a `docker-build` job that runs `docker build -t mcp-server:test . --build-arg BAKE_INDEX=on --secret id=gemini,env=GEMINI_API_KEY` and then `docker run --rm mcp-server:test id -u` (asserts `10001`) and `docker image ls mcp-server:test --format '{{.Size}}'` (asserts < 150 MB). Job gates merge.
- [ ] 4.7 **GREEN — `pyproject.toml` final touch**: add `structlog>=24.1.0` to runtime deps (security/audit needs it) and `tenacity`-free — no new deps for the retry policy. Verify `pip install -e ".[dev]"` still works.
- [ ] 4.8 **REFACTOR — re-run full suite**: `pytest -q --cov=src/mcp_server --cov-fail-under=60` from the local checkout, then `docker build` and `docker run --rm mcp-server:test python -c "import httpx; print(httpx.get('http://localhost:8080/healthz', timeout=4).status_code)"` after the container starts. All green = PR4 ready for review.

## Cross-Phase — Mandatory Gates Before Marking Any Task `[x]`

- [ ] G.1 **Pre-commit hook passes locally**: `pre-commit run --all-files` (gitleaks, ruff lint+format, prettier) — see `.pre-commit-config.yaml`.
- [ ] G.2 **CI `secret-scan.yml` passes**: gitleaks full-history + detect-secrets baseline diff.
- [ ] G.3 **CI `lint.yml` passes**: ruff lint + ruff format --check.
- [ ] G.4 **CI `test.yml` passes**: `pytest -q --cov=src/mcp_server --cov-fail-under=60`.
- [ ] G.5 **Hexagonal invariant test stays green**: `pytest tests/integration/test_hexagonal_invariants.py -q` after every task that adds a new import in `src/mcp_server/`.

## Open Risks (carried into sdd-apply)

| # | Risk | Where it surfaces | Mitigation in tasks |
|---|------|-------------------|---------------------|
| R1 | `docker build` cannot read sibling-project absolute paths in `config/projects.manifest.yaml` → baked index is empty in PR4. | Phase 4 task 4.3 | PR4 only asserts `/healthz` 200; non-empty baked index is deferred to a follow-up change that relocates the manifest into the build context. |
| R2 | Docker `CMD` JSON form does not expand `$PORT`; must use shell form. | Phase 4 task 4.4 | Task uses shell-form `CMD`; `HEALTHCHECK` uses Python `os.environ.get` instead of relying on shell expansion. |
| R3 | `--workers 1` is mandatory for slowapi in-memory state; must be enforced in Dockerfile and tests. | Phase 1 task 1.8 + Phase 4 task 4.4 | `app.py run()` and `Dockerfile CMD` both pass `--workers 1`. |
| R4 | `chunk_hash` MUST include `embedding_dim` to avoid same-hash collision on dim change (ADR-004 follow-up). | Phase 3 task 3.4 | `compute_chunk_hash` is the canonical 5-tuple; unit test 3.3 explicitly covers the dim-change case. |
| R5 | Coverage gate `fail_under = 60` (already in `pyproject.toml`) may trip after PR1 if a partial slice ships. | Phase 1 + Phase 2 | Coverage is gated at the full-PR boundary (`pytest --cov-fail-under=60`); per-PR partial coverage is allowed but documented. |
| R6 | `build_info.py` was the design's planned module for env reads; the single-source-of-env rule says it can't. | Phase 1 task 1.2 | `commit_sha`/`built_at` live in `AppConfig`; no separate `build_info.py`. ADR amendment captured in `sdd-archive` (Phase 0 follow-up). |
| R7 | `--workers 1` keeps slowapi correct only on a single-process deploy; if a future change scales to multi-worker, rate-limit state diverges. | Phase 1 / Phase 4 | Documented in ADR-001 follow-up. Out of scope for 001-bootstrap. |
| R8 | The launch prompt asks for `preindex = "mcp_server.interfaces.cli.preindex:cli"`; ADR-002 exposes `main`. Both must exist. | Phase 3 task 3.20 | `cli()` thin wrapper around `main(argv)`. |
