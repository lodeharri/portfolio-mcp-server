# explain_architecture — Delta Specification

## Purpose

The `explain_architecture` MCP tool reads the Architecture Decision Records
(ADRs) for a declared project and returns a Gemini-generated summary of
the project's architectural choices. A recruiter asks "what's the
architecture of finance-coach-latam?" and gets a synthesized, recruiter-
friendly narrative backed by the on-disk ADRs.

The tool reads the ADR file via the path declared in the project's
manifest entry (the `adr_path` extra field that the YAML adapter already
preserves on disk) and feeds the text to `LLMPort.summarize`. All output
MUST pass through `OutputSanitizer` (Layer 3) because ADRs frequently
reference third-party services (AWS ARN-shaped strings, GitHub repo URLs
with PAT-like fragments, etc.).

The corresponding use case is
`src/mcp_server/application/use_cases/explain_architecture.py::ExplainArchitectureUseCase`.
The FastMCP `@mcp.tool` registration lives in
`src/mcp_server/interfaces/mcp/server.py`.

## Schema / Interface

```python
# src/mcp_server/application/use_cases/explain_architecture.py
from dataclasses import dataclass
from mcp_server.application.ports.llm import LLMPort
from mcp_server.application.ports.manifest import ManifestPort

@dataclass(frozen=True)
class ExplainArchitectureRequest:
    project_id: str                # declared project (e.g. "finance-coach-latam")
    max_tokens: int = 500          # soft upper bound on the LLM summary

@dataclass(frozen=True)
class ExplainArchitectureResult:
    project_id: str
    display_name: str
    summary: str                   # Gemini-generated narrative
    sources: list[str]             # ADR file paths included as citations

class ExplainArchitectureUseCase:
    def __init__(
        self,
        *,
        manifest: ManifestPort,
        llm: LLMPort,
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
    ) -> None: ...

    def execute(
        self, request: ExplainArchitectureRequest
    ) -> ExplainArchitectureResult: ...

# src/mcp_server/interfaces/mcp/server.py — registration
@mcp.tool(name="explain_architecture",
          description="Summarize a project's architecture from its ADRs.")
async def explain_architecture_tool(
    project_id: str, max_tokens: int = 500
) -> dict:
    """Returns {project_id, display_name, summary, sources}.
    Output is sanitized (Layer 3)."""
```

## Requirements

### Requirement: ADR File Is Read From Manifest Metadata

The use case MUST resolve the ADR file path via the `Project` entry's
`adr_path` extra field (read from `projects[].adr_path` in the manifest).
It MUST NOT walk any filesystem path that is not declared in the
manifest.

#### Scenario: ADRs are read for a declared project

- GIVEN the manifest declares `finance-coach-latam` with
  `adr_path: "openspec/changes/initial-poc/design.md"`
- WHEN `explain_architecture(project_id="finance-coach-latam")` is invoked
- THEN the response MUST include `sources=[".../initial-poc/design.md"]`
- AND the `summary` MUST be a non-empty string.

#### Scenario: Unknown project_id raises domain error

- GIVEN the caller passes `project_id="nonexistent"`
- WHEN `explain_architecture` is invoked
- THEN the use case MUST raise `ManifestProjectNotFoundError`
- AND the MCP layer MUST return a JSON-RPC error
  (`code=-32602` invalid params).

### Requirement: LLM Summary Honors the Token Budget

The use case MUST call `LLMPort.summarize(adr_text, max_tokens=request.max_tokens)`
to produce the summary. The LLM adapter's own retry / backoff policy
(ADR-003, mirrored in `GeminiLlmAdapter`) MUST be honored — the use
case MUST NOT swallow transient failures.

#### Scenario: LLM is invoked once per call

- GIVEN a 30 KB ADR file and `max_tokens=500`
- WHEN `explain_architecture` is invoked
- THEN `LLMPort.summarize` MUST be called exactly once
- AND the prompt MUST include the ADR file content
- AND `max_tokens` MUST equal the requested value.

#### Scenario: LLM transient error surfaces as tool error

- GIVEN Gemini returns HTTP 429 three times
- WHEN `explain_architecture` is invoked
- THEN the use case MUST raise `GeminiTransientError`
- AND the MCP layer MUST return a sanitized JSON-RPC internal error
  (`code=-32603`).

### Requirement: Output Passes Through OutputSanitizer (Layer 3)

Both `summary` and `sources` MUST be sanitized by
`OutputSanitizer.sanitize_json(...)` before serialization. The LLM
summary is the highest-risk surface: it is the only tool output that is
fully model-generated and the model has no awareness of the five
`SecretPattern` regexes.

#### Scenario: AWS ARN-shaped substring in summary is redacted

- GIVEN the LLM summary contains `arn:aws:iam::123456789012:user/admin`
  AND an `AKIAIOSFODNN7EXAMPLE` key
- WHEN `explain_architecture` returns the summary
- THEN the response MUST contain `arn:aws:iam::123456789012:user/admin=[REDACTED]`
- AND two `RedactionIncident` records (one for `generic` matching the
  ARN pattern, one for `aws`) MUST be emitted.

#### Scenario: GitHub PAT in summary is redacted

- GIVEN the LLM summary references a `ghp_...` token
- WHEN `explain_architecture` returns the summary
- THEN the response MUST contain `[REDACTED]` in place of the token
- AND `audit.warn("output.redacted", source="explain_architecture", ...)`
  MUST be emitted.

#### Scenario: Clean summary passes through unchanged

- GIVEN the LLM summary contains no matching `SecretPattern`
- WHEN `explain_architecture` is invoked
- THEN the response `summary` MUST equal the LLM output verbatim
- AND `incidents` MUST be empty.

#### Scenario: ADR file path in `sources` is preserved

- GIVEN `sources=[".../initial-poc/design.md"]`
- WHEN `explain_architecture` returns the result
- THEN `sources` MUST equal `[".../initial-poc/design.md"]` unchanged
  (paths cannot carry secrets and are not redacted).

## Error / Edge Cases

- ADR file missing on disk (declared in manifest but file deleted): the
  use case MUST raise `FileNotFoundError`; the MCP layer MUST return a
  JSON-RPC internal error (`code=-32603`).
- ADR file exceeds the LLM context window (typical Gemini context is
  ~1 M tokens, but a project with megabytes of design notes is
  possible): the use case MUST truncate to a safe upper bound (first
  64 KB of the file) and MUST emit `audit.warn("llm.truncated", ...)`.
- `--mock-gemini` mode: `LLMPort.summarize` returns the first
  `max_tokens` words verbatim — tests MUST assert on this deterministic
  contract.
- Concurrent invocations: safe (the use case has no shared mutable
  state and the LLM adapter is stateless per the ADR-003 contract).

## Test Scenarios

| Scenario | Required because |
|---|---|
| ADRs are read for a declared project and listed in `sources` | Tool surface contract |
| Unknown `project_id` raises `ManifestProjectNotFoundError` | Input validation |
| `LLMPort.summarize` is called exactly once with the ADR content | LLM contract |
| AWS-shaped substring in LLM summary is replaced by `[REDACTED]` | **Layer 3** output sanitization |
| `arn:aws:...` ARN-shaped substring in summary is redacted (generic pattern) | **Layer 3** output sanitization |
| GitHub PAT in summary is redacted | **Layer 3** output sanitization |
| Clean summary passes through with empty `incidents` | **Layer 3** non-regression |
| ADR file paths in `sources` are preserved verbatim | Non-redaction of metadata |
| Missing ADR file raises `FileNotFoundError` → JSON-RPC internal error | Defensive default |
| `--mock-gemini` mode returns deterministic first-`max_tokens`-words summary | Testability |
| `explain_architecture` is registered in the FastMCP server's tool list | MCP mount integration |
