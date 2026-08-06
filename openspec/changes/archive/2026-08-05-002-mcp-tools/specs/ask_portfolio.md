# ask_portfolio — Delta Specification

## Purpose

The `ask_portfolio` MCP tool is the meta-tool: it exposes a Pydantic AI
`Agent` (backed by Gemini) that has the **other 5 sibling tools** as
function-calling tools. A recruiter asks an open-ended question ("which
project is closest to a real production deployment?") and the agent
decides which tools to call (`list_projects`, `search_code`,
`explain_architecture`, `summarize_readme`, `get_architecture_diagram`)
and synthesizes a recruiter-grade answer.

The agent is built inside `composition.compose()` after the 5 sibling
use cases are wired so all 5 are available as function-calling tools.
This is the only MCP tool that uses Pydantic AI; the others are direct
`@mcp.tool` registrations wrapping a single use case.

The corresponding use case is
`src/mcp_server/application/use_cases/ask_portfolio.py::AskPortfolioUseCase`.
The FastMCP `@mcp.tool` registration lives in
`src/mcp_server/interfaces/mcp/server.py`.

## Schema / Interface

```python
# src/mcp_server/application/use_cases/ask_portfolio.py
from dataclasses import dataclass
from pydantic_ai import Agent

@dataclass(frozen=True)
class AskPortfolioRequest:
    question: str                  # natural-language recruiter question
    conversation_id: str | None = None  # reserved for future multi-turn

@dataclass(frozen=True)
class AskPortfolioResult:
    answer: str                    # final recruiter-facing reply
    tools_called: list[str]        # audit trail (e.g. ["list_projects", "search_code"])
    conversation_id: str | None    # echoed back if provided

class AskPortfolioUseCase:
    def __init__(
        self,
        *,
        agent: Agent,              # built in composition.compose()
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
        rate_limiter: RateLimiterPort,
    ) -> None: ...

    def execute(
        self, request: AskPortfolioRequest
    ) -> AskPortfolioResult: ...

# src/mcp_server/interfaces/mcp/server.py — registration
@mcp.tool(name="ask_portfolio",
          description="Ask a free-form question about the portfolio.")
async def ask_portfolio_tool(
    question: str, conversation_id: str | None = None
) -> dict:
    """Returns {answer, tools_called, conversation_id}.
    Output is sanitized (Layer 3). Rate limited via slowapi (Layer 5)."""
```

The agent itself is built once in `composition.compose()`:

```python
# src/mcp_server/composition.py — PR3 (002-mcp-tools)
agent = Agent(
    model="google-gla:gemini-2.0-flash",
    tools=[
        list_projects_tool,        # sibling @mcp.tool functions
        search_code_tool,
        explain_architecture_tool,
        summarize_readme_tool,
        get_architecture_diagram_tool,
    ],
    retries=2,                     # ADR-005: cap Pydantic AI retries
    max_tool_calls=5,              # ADR-005: prevent runaway tool loops
)
```

## Requirements

### Requirement: Agent Registers the Five Sibling Tools

The Pydantic AI `Agent` MUST be initialized with the 5 sibling MCP tools
as function-calling tools. The agent MUST NOT be allowed to call any
tool that is not in this list (no network tools, no shell tools).

#### Scenario: All five sibling tools are registered on the agent

- GIVEN the composition root has wired the 5 sibling use cases
- WHEN `ask_portfolio` is invoked
- THEN the agent MUST have exactly 5 tools registered
- AND the tools MUST be `list_projects`, `search_code`,
  `explain_architecture`, `summarize_readme`, and
  `get_architecture_diagram`.

#### Scenario: Unknown tool calls are rejected

- GIVEN the agent (via the Gemini model) requests a tool that is not
  in the registered set (e.g. `Bash`)
- WHEN the agent runtime processes the request
- THEN the request MUST be rejected by Pydantic AI
- AND an `audit.warn("agent.tool_rejected", tool="Bash")` MUST be emitted.

### Requirement: Multi-Step Latency Is Capped

The agent MUST NOT exceed `max_tool_calls=5` per invocation. This is the
defense against Pydantic AI multi-step tool-call loops (the
`002-mcp-tools` proposal's risk register).

#### Scenario: Fifth tool call is the last allowed

- GIVEN the agent has already called 4 sibling tools
- WHEN the model requests a 6th tool call
- THEN the agent MUST abort with `pydantic_ai.exceptions.MaxToolCallsExceeded`
- AND the audit log MUST emit
  `event="agent.max_tool_calls_exceeded"`.

#### Scenario: Clean run finishes within budget

- GIVEN the agent resolves the question using 2 tool calls
- WHEN `ask_portfolio` returns
- THEN `tools_called` MUST equal `["list_projects", "search_code"]`
  (or whatever the agent actually invoked).

### Requirement: Output Passes Through OutputSanitizer (Layer 3)

The agent's final `answer` is fully model-generated and concatenated
from the output of multiple sibling tools. Every byte returned in
`answer` MUST be sanitized via `OutputSanitizer.sanitize(answer, source="ask_portfolio")`
before serialization. This is the **highest-risk redaction surface**
in the system because it is the only tool that aggregates output from
multiple sources.

#### Scenario: AWS-shaped key in aggregated answer is redacted

- GIVEN the agent's `search_code` tool returns a chunk containing
  `AKIAIOSFODNN7EXAMPLE`
- AND the agent quotes that chunk verbatim in its `answer`
- WHEN `ask_portfolio` returns the result
- THEN the response `answer` MUST contain `[REDACTED]` in place of
  the key
- AND a `RedactionIncident` with `pattern=aws` MUST be emitted.

#### Scenario: GitHub PAT in `explain_architecture` summary is redacted

- GIVEN the agent calls `explain_architecture` and the LLM summary
  contains a `ghp_...` token
- WHEN the agent echoes that summary into its `answer`
- THEN the response `answer` MUST contain `[REDACTED]` in place of
  the token
- AND the audit log MUST emit `event="output.redacted"` with
  `source="ask_portfolio"`.

#### Scenario: Clean answer passes through unchanged

- GIVEN the agent's `answer` contains no matching `SecretPattern`
- WHEN `ask_portfolio` returns the result
- THEN the response `answer` MUST equal the agent output verbatim
- AND `incidents` MUST be empty.

### Requirement: Rate Limiter Caps Blasts

The use case MUST call `RateLimiterPort.check(client_ip)` before
invoking the agent. When the limiter returns `False`, the use case
MUST raise a domain error that the MCP layer translates to JSON-RPC
error (`code=-32603`) with a sanitized message.

> Layer 5 already wraps the entire `/mcp` endpoint via slowapi, but
> the agent is the **expensive** endpoint — a 5-tool-call loop
> against Gemini is several cents per request. The application-layer
> check is belt-and-braces against a future router refactor that
> might forget to wire the slowapi exception handler.

#### Scenario: 31st request from the same IP is rejected

- GIVEN the same client IP has already made 30 requests in the last
  60 seconds
- WHEN it sends a 31st request
- THEN the use case MUST raise `RateLimitExceeded`
- AND the MCP layer MUST return JSON-RPC internal error
  (`code=-32603`).

### Requirement: Audit Trail Records Tool Selection

Every tool the agent calls MUST be recorded in the audit log so the
demo recording (the recruiter's screen-share) can later be replayed
to show which sources the agent used.

#### Scenario: Each tool call emits an audit event

- GIVEN the agent calls `list_projects` and then `search_code`
- WHEN `ask_portfolio` returns
- THEN the audit log MUST contain exactly two events:
  `event="agent.tool_call"` with `tool="list_projects"` and
  `event="agent.tool_call"` with `tool="search_code"`.

## Error / Edge Cases

- Pydantic AI transient error (Gemini 429 × retries): the use case MUST
  raise `GeminiTransientError` → JSON-RPC internal error (sanitized).
- Agent raises `MaxToolCallsExceeded`: MUST be caught and translated
  to a recruiter-friendly error (`"the agent needed more tools than
  allowed to answer this question — try a narrower prompt"`).
- Empty `question` argument: MUST raise `ValueError` → JSON-RPC
  invalid params (`code=-32602`).
- `--mock-gemini` mode: the agent is built with a mock model; the
  mock answers with the literal text `f"[mock answer to: {question}]"`
  and calls zero tools. Tests assert on this deterministic contract.
- Concurrent invocations: safe (Pydantic AI agents are stateless per
  invocation; the composition root builds ONE shared agent instance).

## Test Scenarios

| Scenario | Required because |
|---|---|
| All five sibling tools are registered on the agent | Tool composition contract |
| Unknown tool calls are rejected by Pydantic AI | Tool sandboxing |
| 6th tool call aborts with `MaxToolCallsExceeded` | **Agent loop cap** |
| Clean run finishes within `max_tool_calls` budget | Normal-path contract |
| AWS-shaped substring in aggregated `answer` is replaced by `[REDACTED]` | **Layer 3** output sanitization |
| GitHub PAT echoed into `answer` is redacted | **Layer 3** output sanitization |
| OpenAI / Gemini / generic patterns redacted (table-driven) | **Layer 3** output sanitization |
| Clean `answer` passes through with empty `incidents` | **Layer 3** non-regression |
| 31st request from the same IP raises `RateLimitExceeded` | **Layer 5** rate limit |
| Empty `question` raises `ValueError` → JSON-RPC invalid params | Input validation |
| Each agent tool call emits `audit.warn("agent.tool_call", ...)` | Audit trail |
| Gemini 429 surfaces as sanitized JSON-RPC internal error | **Layer 3** error-boundary sanitization |
| `--mock-gemini` mode returns deterministic `[mock answer to: ...]` | Testability |
| `ask_portfolio` is registered in the FastMCP server's tool list | MCP mount integration |
| Hexagonal invariant test stays GREEN (composition root wires agent) | Architecture invariant |
