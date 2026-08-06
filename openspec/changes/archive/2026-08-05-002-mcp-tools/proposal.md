# Proposal: 002-mcp-tools — Implement the 6 MCP Tools

## Intent

`001-bootstrap` shipped scaffolding, 5-layer security, the preindex pipeline,
and an empty FastMCP. The server answers the initialize handshake but
exposes **zero tools** — recruiters have nothing to ask Claude Desktop /
Cursor / MCP Inspector / the future HR playground. This change lights up the
6 tools from `README.md` and the `preindex-pipeline` spec, turning the
scaffold into the demo surface the portfolio exists for.

## Scope

### In Scope
- 6 use cases in `application/use_cases/`: `list_projects`, `search_code`,
  `explain_architecture`, `summarize_readme`, `get_architecture_diagram`,
  `ask_portfolio` (Pydantic AI agent wrapping the other 5).
- Wire 6 use cases in `composition.py`; replace `None` placeholders for
  `search_use_case` / `list_projects_use_case`; add 4 new fields.
- Register 6 tools in `interfaces/mcp/server.py` via `@mcp.tool`.
- Build the Pydantic AI `Agent` inside `composition.compose()` after the 5
  sibling use cases (wiring stays in one place).
- RED-first tests: 6 use-case unit tests, 2-3 MCP-mount integration tests,
  1 E2E smoke via the in-process FastMCP client.
- Hexagonal invariant test MUST stay GREEN.

### Out of Scope
- Playground UI (`003-playground-ui`). Streaming chat (`004-chat-tab`).
- Fly.io deploy / custom domain (`005-deploy`).
- New LLM providers (Gemini only).
- `/mcp` auth (intentional for demo).

## Capabilities

### New
- `list_projects` — manifest read. No LLM.
- `search_code` — `EmbeddingPort.embed_one(query)` + `VectorStorePort.search`.
- `explain_architecture` — read ADRs + `LLMPort.summarize`.
- `summarize_readme` — read `README.md` + `LLMPort.summarize`.
- `get_architecture_diagram` — read SVG + base64-encode. No LLM.
- `ask_portfolio` — Pydantic AI `Agent` registering the 5 sibling tools.

### Modified
None at the spec level — the 4 main specs already describe the behaviour.

## Approach

Three chained PRs (each ≲ 1500 LOC; under the 400-line review budget):

- **PR1 — read-only** (`search_code` + `list_projects`). 2 use cases over
  `manifest` + `vector_store`. Replace `None` placeholders. 2 `@mcp.tool`
  registrations. Tests: 2 unit, 1 integration.
- **PR2 — file readers + LLM** (`explain_architecture` + `summarize_readme` +
  `get_architecture_diagram`). 3 use cases. `gemini_llm.py` already
  implements `summarize()`; no adapter change beyond per-call sanitization.
  3 `@mcp.tool` registrations. Tests: 3 unit, 1 integration.
- **PR3 — agent** (`ask_portfolio`). `use_cases/ask_portfolio.py` wrapping
  `pydantic_ai.Agent(model="google-gla:gemini-2.0-flash", tools=[...])`,
  built in `composition.compose()` after the 5 sibling use cases. Tests:
  1 unit (mock `model_run`), 1 integration (tool registered).

Every tool's output passes through `OutputSanitizer.sanitize()` before
returning to the MCP client (Layer 3 invariant).

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `src/mcp_server/application/use_cases/*.py` | New (5 files) | One use case per tool |
| `src/mcp_server/composition.py` | Modified | Wire 6 use cases; build Pydantic AI agent |
| `src/mcp_server/interfaces/mcp/server.py` | Modified | 6 `@mcp.tool` registrations |
| `tests/unit/application/use_cases/*.py` | New (6 files) | RED-first per use case |
| `tests/integration/test_mcp_tools_*.py` | New (2-3 files) | FastMCP client smoke |

## Risks

| Risk | L | Mitigation |
|------|---|------------|
| Pydantic AI multi-step latency | M | Layer 5 limiter caps blast radius; `--mock-gemini` for tests |
| Rate-limit hits during demo | M | slowapi 30 req/min/IP returns HTTP 429 |
| Agent tool-call loops | M | Pydantic AI `retries=2`; max 5 tool calls per turn |
| Sanitizer over-redacts legit hashes | L | Narrow pattern list; two-direction tests |
| Image > 150 MB after Pydantic AI | M | `pydantic-ai-slim[google]` excludes Anthropic/OpenAI |

## Rollback

Additive. Revert → server reverts to `001-bootstrap` state (empty FastMCP,
`/healthz` still 200, no data loss). PR3 independently revertable.

## Dependencies

- `001-bootstrap` ✅ shipped.
- `pydantic-ai-slim[google]>=2.0.0` ✅ in `pyproject.toml`.
- `fastmcp>=3.2.4` ✅ (verified `@mcp.tool` against 3.4.6).
- Manifest declares the 2 sibling projects ✅.

## Success Criteria

- [ ] All 6 tools callable via the in-process FastMCP client
- [ ] `composition.compose()` builds without `None` placeholders for new use cases
- [ ] Hexagonal invariant test stays GREEN
- [ ] Each tool's output passes through `OutputSanitizer`
- [ ] `pytest -q` passes with coverage ≥ 60 %
- [ ] MCP Inspector smoke: `list_portfolio_projects` returns ≥ 1 project
- [ ] `--mock-gemini` mode end-to-end runnable

## Cost Discipline

Free-tier only. Pydantic AI reuses `gemini-2.0-flash` under Gemini free RPM.
`ask_portfolio` rides the existing slowapi limiter (30 req/min/IP, Layer 5).
No new paid services. Final image < 150 MB.