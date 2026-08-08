# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Known Issues
- Docker image is 417 MB vs original 150 MB target (budget raised to 500 MB)

## [0.1.1] — 2026-08-08 — MCP-UI & chat polish + manifest bake

### Added
- `portfolio-mcp-server` registered in `config/projects.manifest.yaml` as a third project; semantic DB pre-baked (`data/index.sqlite`, 466 chunks) for Fly.io deploy prep
- Chat UI: inline trace pills surfacing agent tool calls as they stream
- Chat UI: terminal-inspired visual design with typed error recovery
- Chat: system prompt + reduced tool-call budget (5 → 3) + recursion headroom for stability

### Changed
- README rewritten to reflect current 5-tools-working state (was the "greenfield" placeholder)
- Architecture section updated to reflect LangChain single-file centralization
- Removed backend/frontend/infra split in favor of hexagonal architecture documentation
- Web UI fully translated to Spanish (templates + i18n override map)

### Removed
- `/playground` browser surface removed — `/mcp-ui` is now the sole recruiter-facing MCP tool surface (-1181 lines)
- Technology footer removed from all pages (recruiter-facing presentation)

### Fixed
- `ask_portfolio` no longer fails with `Function must have a docstring if description not provided` — LangChain adapter composition wired with sibling tools, tool registers and responds correctly (verified live with `mcp.client.streamable_http`)
- `get_architecture_diagram` returns valid SVG data URL when the project has `docs/architecture.svg`; clean `"referenced file not found"` error otherwise (no 500)
- `/mcp-ui` now unwraps the FastMCP `{result: ...}` envelope before dispatching to renderers (4 of 5 renderers were silently broken)
- `/mcp-ui` now surfaces tool parameters via the FastMCP 3.4.6 `.parameters` attribute (was reading the renamed-away `.inputSchema`)
- `/mcp-ui` `truncateSnippet` preserves chunk content (was slicing only first 2 lines, dropping everything for markdown-fenced snippets)
- HTTP 429 from Gemini now translates to a recruiter-readable banner via `GeminiQuotaExceededError` (was opaque stacktrace)
- Chat composer bottom-margin overflow + clear-history button
- Chat history capped + user messages persist on send
- Chat input clears on send (optimistic UI)
- Chat stream skips empty `AIMessageChunk` content shapes and surfaces typed errors instead of silent failures

### Verified
- `pytest -q` → 736 passed, 12 skipped (docker daemon only), 0 failed
- Live MCP session: 6 tools registered, `ask_portfolio` and `get_architecture_diagram` round-trip OK against `gemini-flash-latest`

## [2026-08-05] — LangChain centralization + bug fixes

### Added
- Mock fallback for `ask_portfolio` when composition is not wired (`_MockAskPortfolioUseCase`)
- `LangChainEmbeddingAdapter` in single LangChain file
- `--purge-orphans` flag to preindex CLI for cleaning up stale chunks
- `VectorStorePort.distinct_file_paths(project_id)` and `delete_by_file_path(project_id, file_path)` methods
- Audit event `orphans.purged` JSON output per project

### Changed
- Migrated from `google-generativeai` (deprecated) to `google-genai` (saves ~250 MB in image)
- Switched from `pydantic-ai` to `pydantic-ai-slim[google]` (saves ~50 MB)
- Refined `projects.manifest.yaml` to only include source code + docs (excludes `.yaml`, `.yml`, `.toml`, `.json`)
- Lines across 20+ files to make ruff clean

### Fixed
- `SqliteVecStore.delete_by_file_path` was missing `commit()` so deletes weren't visible to subsequent reads
- `JsonSchema` field types in `AskPortfolioRequest` (was `tool_calls` vs `tools_called` mismatch)
- `Sequence` import missing for `MockAskPortfolioUseCase`
- Various ruff lint errors (import order, unused vars, docstring requirements)

### Commit Stats
- 50+ commits across 5 changes (001-bootstrap, 002-mcp-tools, 005-langchain-integration, 006-search-code-refinement, 007-embeddings-via-langchain)
- 479 tests pass (was 364 at start of session)
- 85.73% coverage
- 1 critical bug remaining (ask_portfolio LangChain binding)

## [2026-08-05] — Initial 001-bootstrap

### Added
- Hexagonal architecture foundation (domain / application / infrastructure / interfaces)
- 5-layer security model (manifest / gitleaks / output sanitizer / pre-commit / rate limiter)
- Preindex pipeline with chunk-hash caching and checkpoint/resume
- Multi-stage Dockerfile with baked schema-only DB
- Fly.io deployment config (~$2/mo)
- 644 tests passing (after fixes)
- 6/6 hexagonal invariants passing
- 85.73% coverage
- 417 MB Docker image (down from 676 MB after subsequent LangChain migration)

### Removed
- Initial "greenfield" placeholder README

## [Earlier] — Initial project

### Added
- Repository scaffolding
- `pyproject.toml` with placeholder dependencies
- `.gitignore` and basic CI workflows
- OpenSpec structure (`openspec/config.yaml`, `openspec/specs/`, `openspec/changes/`)
