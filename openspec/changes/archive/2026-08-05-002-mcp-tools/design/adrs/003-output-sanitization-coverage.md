# ADR 003: Output sanitization coverage — Layer 3 + Layer 5 across the 6 tools

- **Status**: Accepted
- **Date**: 2026-08-05
- **Change**: `002-mcp-tools`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

The 5-layer security model mandates that **every byte that leaves the server** passes through `OutputSanitizer` (Layer 3) and that **every redaction** emits an audit event (Layer 5). The model is enforced today by:

1. `OutputSanitizerMiddleware` (HTTP responses on `/healthz` and any non-`/mcp` route).
2. `OutputSanitizer` injected into the preindex use case (sanitizes the preindex summary log line).

`002-mcp-tools` adds 6 MCP tools. Each returns a structured payload (JSON-serializable dict / list). Each payload has at least one **string field** that is potentially token-bearing:

| Tool | Token-bearing fields | Why |
|---|---|---|
| `list_projects` | `description`, `display_name` | Manifest descriptions can contain env-var examples |
| `search_code` | `content` of every chunk | The chunk text IS the corpus — high density of tokens |
| `explain_architecture` | `summary` | LLM-generated; no knowledge of secret patterns |
| `summarize_readme` | `summary`, `source` | LLM-generated; READMEs have `.env.example` blocks |
| `get_architecture_diagram` | `data` (decoded SVG) | SVG `<text>` and `<script>` can carry tokens |
| `ask_portfolio` | `answer` | Aggregated, model-generated |

The question is: where does the sanitization call live for each tool, and what about the Pydantic AI agent (which is fundamentally different — its output is generated, not retrieved)?

## Decision Drivers

- **D1**: Every byte that reaches the MCP client is redacted (Layer 3 invariant).
- **D2**: Every redaction emits exactly one `output.redacted` event (Layer 5 invariant) — not one per match.
- **D3**: Sanitization at the source, not at the boundary — the use case owns the call so the wrapper can stay a thin ~10-line shim.
- **D4**: Defense-in-depth for the agent: even if a sibling tool somehow leaks (e.g. a future use case skips sanitization), the agent's final `answer` is still sanitized before reaching the MCP client.
- **D5**: No double-sanitization cost concerns. The sanitizer's regex pass is idempotent — sanitized text stays sanitized.

## Considered Options

### Option A — Sanitize inside every use case + sanitize agent's final answer (chosen)

Each of the 6 use cases receives `OutputSanitizer` via constructor. Right before returning, it calls `sanitizer.sanitize(...)` (or `sanitize_json(...)`) on every token-bearing field. The audit emission is automatic (the sanitizer emits one `output.redacted` event per `sanitize()` call that finds matches).

For the agent: `AskPortfolioUseCase.execute()` sanitizes `agent.run(question).output` (the final text) with `source="ask_portfolio"`, then returns. This is **belt-and-braces** — sibling tools already returned sanitized payloads, but the agent's aggregation is also sanitized in case a future sibling tool slips.

**Pros**:
- Use case owns sanitization (single responsibility). The wrapper stays a ~10-line shim.
- Tests inject a fake sanitizer and assert on `SanitizedOutput.incidents` per use case.
- Layer 3 invariant is satisfied at the source; the boundary can still have a defense-in-depth middleware.
- The agent case is a natural extension — same call, different `source` label.

**Cons**:
- Each use case must remember to call `sanitize` before returning. A future use case author who forgets breaks the invariant. Mitigated by a wrapper-level fallback (see "Defense-in-depth fallback" below).
- The agent sanitizes text that was already sanitized by sibling tools — minor redundant work (the regex pass is fast; sub-millisecond for typical outputs).

### Option B — Sanitize at the MCP boundary (rejected)

A single FastMCP middleware runs `sanitize_json(...)` on every tool response before serialization.

**Pros**:
- One place. Use cases stay pure.

**Cons**:
- FastMCP middleware applies to ALL responses, including binary SVG bytes (`get_architecture_diagram.data` is base64). Calling `sanitize_json` on base64 is a no-op for the patterns (regex won't match base64 alphabet), but it's wasted work.
- The `source=` label becomes less meaningful — "tool response" instead of "search_code" / "explain_architecture" / etc. Audit logs lose granularity.
- Defense-in-depth is lost: if the boundary is bypassed (e.g. a future streaming endpoint), nothing protects the output.

### Option C — Sanitize only at the boundary for some tools, inside use cases for others (rejected)

The LLM-backed tools (`explain_architecture`, `summarize_readme`, `ask_portfolio`) sanitize inside the use case (model-generated text is the highest-risk surface). The read-only tools sanitize at the boundary.

**Pros**:
- Aligns "where to sanitize" with "how risky is the output".

**Cons**:
- Two code paths to maintain. The risk categorization is subjective (a `search_code` chunk with an AWS key in `auth.py` is HIGH risk).
- A future use case author must categorize their tool before writing code.
- Tests must cover both paths.

## Decision

**Option A.** Sanitize inside every use case + sanitize agent's final answer.

Per-tool sanitization surface:

| Tool | Sanitize call | `source=` label |
|---|---|---|
| `list_projects` | `sanitize_json(payload, source="list_projects")` once per call | `list_projects` |
| `search_code` | `sanitize(result.content, source="search_code")` per chunk; metadata fields pass through | `search_code` |
| `explain_architecture` | `sanitize(summary, source="explain_architecture")` after `llm.summarize()`; `sanitize_json` over the full result | `explain_architecture` |
| `summarize_readme` | same shape as above | `summarize_readme` |
| `get_architecture_diagram` | `data: bytes → str.decode("utf-8") → sanitize → str.encode → b64encode` (decode-sanitize-reencode cycle) | `get_architecture_diagram` |
| `ask_portfolio` | `sanitize(answer, source="ask_portfolio")` on the agent's final text | `ask_portfolio` |

**Defense-in-depth fallback**: the existing `OutputSanitizerMiddleware` (registered on the parent FastAPI app, currently applied to `/healthz`) is extended to also apply to `/mcp/*` responses that don't go through a wrapper-level sanitize. This is a safety net for future tool authors who forget the use-case-level call. The middleware skips responses whose `source` label is already one of the 6 tools (idempotency check) — actually, simpler: just let it run. The sanitizer is idempotent; running it twice is safe but doubles the audit emission count. To avoid the audit double-count, the middleware checks for a custom header `X-Sanitizer-Source: list_projects` set by the use case; if present, the middleware skips.

**No input sanitization for the agent**. The agent's input (the recruiter's question) is user-controlled and intentionally allows arbitrary text. Sanitizing it would strip legitimate code samples that the recruiter might paste. The agent's safety boundary is at the OUTPUT, not the input.

## Consequences

**Positive**:
- Each use case has a single, testable sanitization boundary.
- Audit logs carry the per-tool `source` label — recruiter demo replays can show which tool redacted which match.
- The agent's output is sanitized even if a sibling tool slips (defense-in-depth).
- The middleware fallback catches future use cases that forget the source-level call.

**Negative**:
- Two `sanitize` calls in the agent path (one per sibling tool result, one on the aggregated answer). The redundant cost is sub-millisecond.
- The middleware "skip-if-already-sanitized" header trick is a small piece of magic. Documented inline. Alternative: just accept the doubled audit emission (matches = 2× incidents). Decision: use the header to keep audit counts truthful.
- A future use case author must know to inject `sanitizer` and call it. Mitigated by:
  - The middleware fallback (Layer 3 invariant preserved at the boundary).
  - A unit test that asserts each wrapper's response passes through the middleware (catch regressions early).

## Compliance with rules

- `invariants` → "All MCP tool outputs pass through OutputSanitizer before reaching the client" — satisfied; every use case sanitizes, middleware catches the rest.
- `invariants` → "5-layer security model is mandatory" — Layer 3 + Layer 5 both invoked at use case boundary.
- `rules.specs` → "Any new tool MUST include a security redaction test scenario" — every delta spec in `002-mcp-tools/specs/` has 4+ redaction scenarios. ADR-003's coverage table is the implementation map.

## Follow-ups

- In apply phase: write `tests/unit/security/test_output_sanitizer_coverage.py` parametrizing `(tool_name, payload_with_token, expected_redaction)` for all 6 tools.
- In apply phase: extend `OutputSanitizerMiddleware` to check `X-Sanitizer-Source` header and skip when present.
- In verify phase: end-to-end smoke against `/mcp` confirming audit JSON has one `output.redacted` event per `sanitize` call (not two).
