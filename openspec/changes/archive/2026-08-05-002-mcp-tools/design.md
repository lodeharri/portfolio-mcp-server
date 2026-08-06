# Design: 002-mcp-tools — Implement the 6 MCP Tools

## Technical Approach

Eager composition root (continues ADR-001). Composition wires 6 use cases plus a Pydantic AI `Agent` that re-uses the 5 sibling `@mcp.tool` functions as function-calling tools (ADR-001). Each use case receives `OutputSanitizer` + `AuditLogger` via constructor and sanitizes its return value at the source. `interfaces/mcp/tool_errors.py` (NEW) maps `DomainError` subclasses to JSON-RPC codes (ADR-002). Three chained PRs (PR1 = read-only, PR2 = file-readers+LLM, PR3 = agent) match the proposal's review budget.

## Architecture Overview

Hexagonal layers unchanged from 001; new surface is `application/use_cases/` (6 files) + `interfaces/mcp/server.py` (6 `@mcp.tool` registrations). `OutputSanitizer` injected into every use case → sanitization at source, before wrapper returns. `security/`, `domain/`, `infrastructure/adapters/`, `application/ports/` untouched.

```
   interfaces/mcp/server.py   ──  @mcp.tool × 6 (5 reused as agent tools)
            │
   application/use_cases/  ──  6 NEW classes (one per tool)
            │
   application/ports/ + domain/  ──  unchanged
            │
   composition.py  ──  NOW wires 6 use cases + Agent
            │
   infrastructure/adapters/ + security/  ──  unchanged
```

## Sequence Diagram — MCP Request Lifecycle

```
client (MCP) ──POST /mcp──► FastAPI ──► /mcp sub-app (FastMCP)
                                              │
                                  FastMCP tool dispatcher
                                  e.g. search_code_tool
                                              │
                                  @mcp.tool wrapper (try/except)
                                    rate_limiter.check(client_ip)  ← Layer 5
                                    use_case.execute(request)
                                    translate domain errors via tool_errors.py
                                              │
                                  SearchCodeUseCase.execute()
                                    1. embed query → vector
                                    2. vector_store.search(vector, limit)
                                    3. sanitizer.sanitize_json(result, source)  ← Layer 3
                                              │
                                  sanitizer → audit.warn("output.redacted", ...) ← Layer 5
                                              ▼
                                  JSON-RPC response → FastMCP → client (MCP)
```

## Sequence Diagram — Secret-Redaction Flow (Layer 3 + Layer 5)

```
tool output (LLM summary, chunk content, SVG text)
                              │
                              ▼
        OutputSanitizer.sanitize(text, source=tool)
          for pattern, regex in _PATTERN_TABLE:
            re.sub(pattern, "[REDACTED]", text)
              → records RedactionIncident(pattern, start, end)
                              │
                              ▼
        if incidents and audit:        ← Layer 5
          audit.warn("output.redacted",
                     source=tool, count=N, patterns="aws,github")
                              │
                              ▼
                  redacted text → wire

  Write-time parallel (Layer 2):
    chunk text → GitleaksScanner.scan(chunk, src)
       exit 0 → CLEAN · exit 1 → BLOCKED + audit("secret.blocked")
       exit 2 → FLAGGED + audit("secret.flagged") + insert with flag=1
```

## Sequence Diagram — Pydantic AI Agent Orchestration (`ask_portfolio`)

```
client ──ask_portfolio("where did I implement rate limiting?")──►
   FastMCP dispatcher ──► ask_portfolio_tool(question)
       │
       │  rate_limiter.check(client_ip)  ──False──► RateLimitExceeded → JSON-RPC -32603
       ▼
   AskPortfolioUseCase.execute(req)
       │
       │  agent = pydantic_ai.Agent(                ← built once in composition.compose()
       │      model="google-gla:gemini-2.0-flash",
       │      tools=[list_projects_tool, search_code_tool,
       │             explain_architecture_tool, summarize_readme_tool,
       │             get_architecture_diagram_tool],
       │      retries=2, max_tool_calls=5)           ← ADR-001
       │
       │  loop (max 5 tool calls):
       │     model → tool_call("search_code", {"query": "rate limiting"})
       │     ──► search_code_tool() ──► SearchCodeUseCase ──► sanitized payload
       │     model → final_text_answer
       │
       ▼
   sanitized = sanitizer.sanitize(answer, source="ask_portfolio")  ← Layer 3 defense-in-depth
   audit.warn("agent.tool_call", tool=...)  × per call
   return {answer, tools_called, conversation_id}
```

## Hexagonal Layer Mapping

| Layer | What's new | Depends on |
|---|---|---|
| Domain (pure) | nothing — existing `CodeChunk`, `SearchResult`, `Project` reused | — |
| Application/use_cases | 6 NEW files: `list_projects`, `search_code`, `explain_architecture`, `summarize_readme`, `get_architecture_diagram`, `ask_portfolio` | ports + sanitizer + audit |
| Application/ports | unchanged (all 6 ports exist) | — |
| Infrastructure/adapters | unchanged — `gemini_llm.summarize()` already implements `LLMPort.summarize` | domain |
| Security | unchanged | — |
| Interfaces/mcp/server.py | +6 `@mcp.tool` registrations | composition (use cases indirectly) |
| Interfaces/mcp/tool_errors.py | NEW — central `translate_tool_error(exc) → JSON-RPC code` | domain exceptions |
| Composition | +6 use cases + Pydantic AI Agent | adapters + use cases |

## Composition Wiring Strategy (delta to `composition.py`)

```python
# Adds 4 use cases + Agent; replaces the four None placeholders.
list_projects_uc = ListProjectsUseCase(
    manifest=manifest, vector_store=vector_store,
    sanitizer=sanitizer, audit=audit)
search_uc = SearchCodeUseCase(
    embedding=embedding, vector_store=vector_store,
    sanitizer=sanitizer, audit=audit)
explain_uc = ExplainArchitectureUseCase(manifest=manifest, llm=llm, sanitizer=sanitizer, audit=audit)
summarize_uc = SummarizeReadmeUseCase(manifest=manifest, llm=llm, sanitizer=sanitizer, audit=audit)
diagram_uc = GetArchitectureDiagramUseCase(manifest=manifest, sanitizer=sanitizer, audit=audit)
agent = _build_pydantic_agent(model_name=..., tool_funcs=[...5 siblings...])
ask_portfolio_uc = AskPortfolioUseCase(
    agent=agent, sanitizer=sanitizer, audit=audit, rate_limiter=rate_limiter)
```

`Composition` gains 4 fields (`explain_architecture_use_case`, `summarize_readme_use_case`, `get_architecture_diagram_use_case`, `ask_portfolio_use_case`); the two existing `None` placeholders become real instances. `_build_pydantic_agent` does a lazy import of the 5 sibling `@mcp.tool` functions to avoid the cyclic import (`interfaces.mcp.server` ← → `composition`). The agent is built once and shared.

## MCP Tool Registration Pattern

`@mcp.tool` is a FastMCP decorator (3.2.4+, verified 3.4.6) that introspects the function signature, builds a JSON schema for the arguments, and registers the function in the global tool registry. Calling the function in-process works as a normal async call. Pattern per tool:

```python
@mcp.tool(name="search_code", description="Semantic search over indexed code chunks.")
async def search_code_tool(query: str, top_k: int = 5, project_id: str | None = None) -> list[dict]:
    try:
        results = _composition.search_use_case.execute(SearchCodeRequest(...))
    except DomainError as exc:
        raise ToolError(translate_tool_error(exc))   # → FastMCP → JSON-RPC
    return [r.model_dump() for r in results]
```

All 6 wrappers live in `interfaces/mcp/tools.py` (~10 lines each). The Pydantic AI agent (PR3) imports the 5 sibling wrappers to register them as function-calling tools — same code path, no duplication (ADR-001).

## Per-Tool Capability Summary

| Tool | LLM? | Layer 3 surface | Top error → JSON-RPC code |
|---|---|---|---|
| `list_projects` | No | `description`, `display_name` | — |
| `search_code` | embed only | `content` of every chunk | `GeminiTransientError → -32603` |
| `explain_architecture` | `llm.summarize` | `summary` | `ManifestProjectNotFoundError → -32602` |
| `summarize_readme` | `llm.summarize` | `summary` | `ManifestProjectNotFoundError → -32602` |
| `get_architecture_diagram` | No | decoded SVG bytes | `ValueError → -32603` |
| `ask_portfolio` | + 5 tools | aggregated `answer` | `RateLimitExceeded → -32603` |

Use cases live in `application/use_cases/<tool>.py`. Wrappers in `interfaces/mcp/tools.py` call `use_case.execute(...)` inside `try/except DomainError → ToolError(translate_tool_error(exc))`.

## Test Strategy

| Layer | Scope | Approach |
|---|---|---|
| Unit (application) | 6 use cases | Inject fake ports; assert sanitization happened via `SanitizedOutput.incidents` |
| Unit (security) | adversarial samples | Parametrize over 5 patterns × 6 tools |
| Unit (interfaces/mcp) | 6 wrappers | Assert each wrapper calls its use case once + propagates `translate_tool_error` mapping |
| Integration | FastMCP client + 6 tools | `httpx.AsyncClient` against `create_app()`; each tool returns sanitized output |
| Integration | Composition root | All 6 use-case fields non-`None`; Agent built with 5 tools; hexagonal invariant still GREEN |
| E2E | in-process FastMCP `Client` (per FastMCP 3.4.6 docs) | One tool per capability smoke; `--mock-gemini` mode runs end-to-end |

Tests mirror `src/` (`tests/unit/application/use_cases/`, `tests/unit/interfaces/mcp/`, `tests/integration/`).

## Performance Considerations

- **Agent latency**: 1-5 sequential LLM rounds per `ask_portfolio`. Free-tier Gemini 2.0 Flash ~700 ms/round → worst-case ~3.5 s. Layer 5 limiter caps blast radius; `max_tool_calls=5` caps per-call cost.
- **`search_code`**: one `embed([query])` call (~250 ms cold, ~100 ms warm). No 0.1 s pacing needed (single query, not preindex batch).
- **SVG base64**: 1.5 MB SVG → ~2 MB JSON → within FastMCP body budget. Larger SVGs rejected by `ValueError` (10 MB cap).
- **Cold start**: agent built once in `composition.compose()` (~5 ms). No per-request cost.
- **Memory**: Pydantic AI adds ~10 MB on top of ~95 MB existing footprint. Comfortable under 256 MB Fly VM.

## Threat Matrix

Per `references/threat-matrix.md` — none of the boundaries apply to 002-mcp-tools (no new subprocess, no new path traversal, no new VCS/PR automation). The sanitizer is the existing Layer 3/5 boundary. The only new data flow is the agent's model output, sanitized at the use case boundary (ADR-003). Recorded as N/A.

## Migration / Rollout

Additive. Revert → server returns to `001-bootstrap` state (empty FastMCP, `/healthz` 200, no data loss). PR1 → PR2 → PR3 chain keeps each PR ≲ 1500 LOC, under the 400-line review budget per change.

## Open Questions (resolved — see ADRs)

- **Q1** Agent tool registration → `adrs/001-pydantic-ai-agent-tool-registration.md`
- **Q2** Domain-error → JSON-RPC translation → `adrs/002-tool-error-translation.md`
- **Q3** Layer 3 sanitization coverage → `adrs/003-output-sanitization-coverage.md`
- **Q4** Should agent input also be sanitized? → No, only output. Input sanitization would strip legitimate code samples.

## ADRs

- [`adrs/001-pydantic-ai-agent-tool-registration.md`](adrs/001-pydantic-ai-agent-tool-registration.md)
- [`adrs/002-tool-error-translation.md`](adrs/002-tool-error-translation.md)
- [`adrs/003-output-sanitization-coverage.md`](adrs/003-output-sanitization-coverage.md)
