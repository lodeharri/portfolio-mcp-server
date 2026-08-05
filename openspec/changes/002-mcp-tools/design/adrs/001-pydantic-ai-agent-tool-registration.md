# ADR 001: Pydantic AI Agent tool registration

- **Status**: Accepted
- **Date**: 2026-08-05
- **Change**: `002-mcp-tools`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

`ask_portfolio` is the meta-tool: it exposes a Pydantic AI `Agent` (backed by Gemini) that has the **other 5 sibling tools** as function-calling tools. The agent must be able to call exactly `list_projects`, `search_code`, `explain_architecture`, `summarize_readme`, `get_architecture_diagram` — and nothing else. The tool schemas (name, description, argument JSON-schema) must be identical to what an MCP client sees over `/mcp`, so that "what the recruiter gets from Claude Desktop" matches "what the agent calls internally".

The natural source of those schemas is the `@mcp.tool`-decorated functions in `interfaces/mcp/server.py`. Pydantic AI's `Agent(tools=[...])` accepts plain async callables and introspects them. The question is: do we pass the same `@mcp.tool` functions to the Agent, or do we duplicate them as separate "agent tool" functions?

## Decision Drivers

- **D1**: Schema parity — the agent's view of each tool must equal the MCP client's view. Any drift would let the agent call signatures the MCP layer rejects (or vice versa).
- **D2**: Single source of truth for tool logic. If `search_code_tool` adds a new field, the agent must see it automatically.
- **D3**: Cyclic import avoidance — `interfaces/mcp/server.py` (which hosts `@mcp.tool`) and `composition.py` (which builds the Agent) sit in different branches of the import graph; importing one from the other at module-load time is fragile.
- **D4**: Build cost — building the agent must not become a measurable cold-start tax.

## Considered Options

### Option A — Pass the 5 sibling `@mcp.tool` functions to `Agent(tools=[...])` (chosen)

`Agent` accepts the 5 wrappers directly. Pydantic AI introspects each function's signature (return type, parameter types, docstring) and builds its own tool schema. The `@mcp.tool` decorator on the same function has already registered it with FastMCP — two registrations of the same callable, no conflict.

**Pros**:
- Schema parity is automatic (one source of truth).
- No duplication: changes to a tool's signature propagate to both MCP and agent views.
- The agent's tool registry is a literal `tools=[list_projects_tool, …]` — grep-able.

**Cons**:
- Circular import risk: `composition.py` imports `interfaces.mcp.server` to reach the wrappers; `interfaces.mcp.server` is imported by `composition.py` for wiring. Solved with a lazy import inside a `_build_pydantic_agent()` helper called from `create_composition()` body (not at module-load time).
- The Agent sees the MCP-wrapped signatures (return type `list[dict]`, parameter types `str | None`) rather than the use case's domain types. Acceptable: the agent calls the wrappers as a black box; only JSON shapes matter.

### Option B — Separate "agent tool" functions (rejected)

Define 5 new `async def agent_list_projects(...)` functions in `application/use_cases/` or `interfaces/mcp/`, each delegating to its use case. Pass these to `Agent(tools=[...])`.

**Pros**:
- No cyclic import concern (functions live in `application/`, far from `interfaces/mcp/`).
- Agent can have a slightly different signature (richer return type, extra kwargs).

**Cons**:
- **Schema drift**: if a `@mcp.tool` signature changes, the agent's view silently diverges. The agent may call the function with arguments that the MCP layer would reject.
- 5 extra functions to maintain — duplication directly violates DRY.
- Tests must assert schema equivalence between the two views.

### Option C — Per-request agent (rejected)

Build a fresh `Agent` on every `ask_portfolio` call. Use case receives a "tool factory" and instantiates the 5 wrappers inside `execute()`.

**Pros**:
- Zero import-graph coupling.

**Cons**:
- ~50 ms per call × every `ask_portfolio` request — measurable cold-start tax.
- Agent has no shared state between requests (acceptable but pointless to throw away).
- Tests must construct a factory in every fixture.

## Decision

**Option A.** Pass the 5 sibling `@mcp.tool` functions to the Agent. `_build_pydantic_agent()` does a lazy import:

```python
# composition.py (signature only)
def _build_pydantic_agent(*, model_name: str, tools: list[Callable]) -> Agent:
    from pydantic_ai import Agent
    return Agent(
        model=model_name,
        tools=tools,
        retries=2,
        max_tool_calls=5,   # ADR-001 follow-up: cap Pydantic AI loops
    )
```

`create_composition()` calls `_build_pydantic_agent(tools=[list_projects_tool, search_code_tool, …])` near the end of the function body, after the 5 sibling use cases are constructed and their `@mcp.tool` decorators have already registered them (the decorators run at module-load time when `interfaces.mcp.server` is first imported).

The `retries=2` + `max_tool_calls=5` knobs are Pydantic AI's built-in loop controls. They map directly to the proposal's risk register:
- "Pydantic AI multi-step latency" → bounded by `max_tool_calls`.
- "Agent tool-call loops" → bounded by `retries`.

## Consequences

**Positive**:
- One source of truth for each tool's schema and behavior.
- The agent's tool list reads as a literal in `composition.py` — easy to audit.
- `max_tool_calls=5` is enforced by Pydantic AI's runtime, not by our wrapper code.

**Negative**:
- `_build_pydantic_agent` must use a lazy import. Documented in code via a comment; tests must NOT call it at module-load time (a fixture-level import is fine).
- If a future change wants the agent to call a tool that MCP clients should NOT see, this design breaks down. At that point we'd switch to Option B. Documented as a follow-up boundary.

## Compliance with rules

- `rules.apply.guidelines` → "Hexagonal architecture is mandatory … composition.py is the ONLY module that wires concrete adapters to use cases." — The agent is built in `composition.py`; use cases are referenced through the wrappers which live in `interfaces/mcp/server.py`. Composition remains the only wiring point.
- `invariants` → "All MCP tool outputs pass through OutputSanitizer before reaching the client" — the wrappers call the use cases which sanitize; the agent sees sanitized payloads and never raw secrets. ADR-003 covers this.
- `rules.specs` → "Any new tool MUST include a security redaction test scenario" — see `ask_portfolio` spec scenarios covering the aggregated-answer redaction case.

## Follow-ups

- In apply phase: write `tests/integration/test_agent_registers_sibling_tools.py` asserting the Agent has exactly 5 tools registered with the expected names.
- In apply phase: write `tests/unit/application/use_cases/test_ask_portfolio.py` using `pydantic_ai.models.function.FunctionModel` for deterministic mock answers (verified in `__main__` smoke).
- In verify phase: log the agent's `tools_called` list from a real recruiter demo so the audit trail can be replayed.
