# Tasks: 002-mcp-tools — Implement the 6 MCP Tools

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

## Phase 0 — Hexagonal Invariant (RED → GREEN)

- [ ] 0.1 Extend `tests/integration/test_hexagonal_invariants.py` to assert `compose()` wires all 6 use case fields + the Agent (no `None`). Flip `test_composition_wiring.py::test_search_use_case_is_none` / `::test_list_projects_use_case_is_none` to assert real instances. RED against current code, GREEN after Phase 1–3.

## Phase 1 — PR1: Read-Only Tools (`list_projects`, `search_code`)

- [ ] 1.1 RED→GREEN `application/use_cases/list_projects.py::ListProjectsUseCase` + `tests/unit/application/use_cases/test_list_projects.py` — manifest read, optional vector_store for chunk counts, 5-pattern redaction, empty manifest → `[]`, `audit.warn("output.redacted")`.
- [ ] 1.2 RED→GREEN `application/use_cases/search_code.py::SearchCodeUseCase` + tests — embed→search, top-k order, project_id filter, empty query ValueError, top_k>50 cap, multi-pattern redaction on chunk `content`.
- [ ] 1.3 RED→GREEN `interfaces/mcp/tool_errors.py::translate_tool_error` (NEW, ADR-002) + `tests/unit/interfaces/mcp/test_tool_errors.py` — parametrize over `ManifestProjectNotFoundError`/`ValueError` → `-32602`; `FileNotFoundError`/`GeminiTransientError`/`EmbeddingDimensionMismatchError`/`RateLimitExceeded` → `-32603`; unknown `DomainError` → `-32603`; programming errors re-raised.
- [ ] 1.4 RED→GREEN `interfaces/mcp/tools.py` (NEW) — `list_projects_tool` + `search_code_tool` wrappers (~10 lines each), `try/except DomainError → raise translate_tool_error(exc)`.
- [ ] 1.5 GREEN `composition.py` — instantiate 2 use cases; replace `None` for `list_projects_use_case` + `search_use_case`.
- [ ] 1.6 GREEN `interfaces/mcp/server.py` — `from mcp_server.interfaces.mcp import tools as _tools` to fire decorator registration.
- [ ] 1.7 GREEN `tests/integration/test_mcp_mount.py` — assert both tools in `await client.list_tools()`.

## Phase 2 — PR2: File Readers + LLM (`explain_architecture`, `summarize_readme`, `get_architecture_diagram`)

- [ ] 2.1 Extend `domain/entities.py::Project` + `infrastructure/adapters/yaml_manifest.py::_RawProject` + adapter `load()` — preserve `adr_path`, `readme_path`, `diagram_path` (Pydantic `extra='ignore'` drops them); `tests/unit/infrastructure/adapters/test_yaml_manifest.py` RED→GREEN.
- [ ] 2.2 RED→GREEN `application/use_cases/explain_architecture.py::ExplainArchitectureUseCase` + tests — ADR via `adr_path`, `LLMPort.summarize` once, sanitize `summary`, `sources` verbatim, missing ADR → `FileNotFoundError`, truncate to 64 KB.
- [ ] 2.3 RED→GREEN `application/use_cases/summarize_readme.py::SummarizeReadmeUseCase` + tests — README via `readme_path`, default `max_tokens=300`, `api_key=` redaction, `display_name` fallback to `id`, truncate to 32 KB.
- [ ] 2.4 RED→GREEN `application/use_cases/get_architecture_diagram.py::GetArchitectureDiagramUseCase` + tests — SVG base64, decode→sanitize→re-encode, >10MB `ValueError`, non-SVG prefix reject, `<script>`/`<text>`/`<!-- -->` redaction.
- [ ] 2.5 GREEN extend `interfaces/mcp/tools.py` — append `explain_architecture_tool`, `summarize_readme_tool`, `get_architecture_diagram_tool` wrappers.
- [ ] 2.6 GREEN `composition.py` — wire 3 use cases; add fields (real types, no `object | None`).
- [ ] 2.7 GREEN `tests/integration/test_mcp_tools_readers.py` — FastMCP client smoke under `--mock-gemini` for the 3 tools.

## Phase 3 — PR3: Pydantic AI Agent (`ask_portfolio`)

- [ ] 3.1 RED→GREEN `application/use_cases/ask_portfolio.py::AskPortfolioUseCase` + tests with `pydantic_ai.models.function.FunctionModel` — rate limiter pre-check, sanitize `answer`, audit `agent.tool_call`, `MaxToolCallsExceeded` mapping, empty question → `ValueError`.
- [ ] 3.2 GREEN `composition.py` — `_build_pydantic_agent(model_name="google-gla:gemini-2.0-flash", tools=[5 sibling wrappers], retries=2, max_tool_calls=5)` (lazy import, ADR-001); wire `AskPortfolioUseCase` with `rate_limiter`; add `ask_portfolio_use_case` field.
- [ ] 3.3 GREEN extend `interfaces/mcp/tools.py` — append `ask_portfolio_tool` wrapper.
- [ ] 3.4 GREEN `tests/integration/test_agent_registers_sibling_tools.py` — Agent has exactly 5 named tools.
- [ ] 3.5 GREEN `tests/integration/test_mcp_tools_agent.py` — e2e smoke under `--mock-gemini`.

## Cross-Phase Gates (mandatory before any task is marked [x])

- [ ] G1 `pre-commit run --all-files` exits 0 (ruff + gitleaks).
- [ ] G2 `pytest -q` exits 0; coverage ≥ 60% on `src/mcp_server/`.
- [ ] G3 CI `secret-scan` workflow green on PR head SHA.
- [ ] G4 In-process FastMCP `Client` smoke: `await client.list_tools()` returns all 6 tool names.
