# summarize_readme — Delta Specification

## Purpose

The `summarize_readme` MCP tool reads a declared project's `README.md`
and returns a Gemini-generated, recruiter-friendly summary. A recruiter
asks "what does landing-page-portfolio do?" and gets a one-paragraph
narrative without scrolling through the full file.

The tool reads the README via the path declared in the project's
manifest entry (`readme_path` extra field) and feeds the text to
`LLMPort.summarize`. All output MUST pass through `OutputSanitizer`
(Layer 3) — READMEs frequently include setup instructions with
placeholder credentials, environment variable examples, and links to
internal dashboards.

The corresponding use case is
`src/mcp_server/application/use_cases/summarize_readme.py::SummarizeReadmeUseCase`.
The FastMCP `@mcp.tool` registration lives in
`src/mcp_server/interfaces/mcp/server.py`.

## Schema / Interface

```python
# src/mcp_server/application/use_cases/summarize_readme.py
from dataclasses import dataclass
from mcp_server.application.ports.llm import LLMPort
from mcp_server.application.ports.manifest import ManifestPort

@dataclass(frozen=True)
class SummarizeReadmeRequest:
    project_id: str                # declared project
    max_tokens: int = 300          # tighter than explain_architecture

@dataclass(frozen=True)
class SummarizeReadmeResult:
    project_id: str
    display_name: str
    summary: str                   # Gemini-generated narrative
    source: str                    # README path included as a citation

class SummarizeReadmeUseCase:
    def __init__(
        self,
        *,
        manifest: ManifestPort,
        llm: LLMPort,
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
    ) -> None: ...

    def execute(
        self, request: SummarizeReadmeRequest
    ) -> SummarizeReadmeResult: ...

# src/mcp_server/interfaces/mcp/server.py — registration
@mcp.tool(name="summarize_readme",
          description="Summarize a project's README in recruiter-friendly prose.")
async def summarize_readme_tool(
    project_id: str, max_tokens: int = 300
) -> dict:
    """Returns {project_id, display_name, summary, source}.
    Output is sanitized (Layer 3)."""
```

## Requirements

### Requirement: README Is Read From Manifest Metadata

The use case MUST resolve the README path via the `Project` entry's
`readme_path` extra field (read from `projects[].readme_path` in the
manifest). It MUST NOT walk any filesystem path that is not declared
in the manifest.

#### Scenario: README is read for a declared project

- GIVEN the manifest declares `landing-page-portfolio` with
  `readme_path: "README.md"` (resolved relative to the project root)
- WHEN `summarize_readme(project_id="landing-page-portfolio")` is invoked
- THEN the response MUST include `source="<project_root>/README.md"`
- AND the `summary` MUST be a non-empty string.

#### Scenario: Unknown project_id raises domain error

- GIVEN the caller passes `project_id="nonexistent"`
- WHEN `summarize_readme` is invoked
- THEN the use case MUST raise `ManifestProjectNotFoundError`
- AND the MCP layer MUST return a JSON-RPC error
  (`code=-32602` invalid params).

### Requirement: LLM Summary Honors the Token Budget

The use case MUST call `LLMPort.summarize(readme_text, max_tokens=request.max_tokens)`
to produce the summary. The default `max_tokens=300` (tighter than
`explain_architecture`'s 500) reflects that READMEs are typically
shorter and recruiter summaries should be punchy.

#### Scenario: LLM is invoked once with the README content

- GIVEN a 10 KB README and `max_tokens=300`
- WHEN `summarize_readme` is invoked
- THEN `LLMPort.summarize` MUST be called exactly once
- AND the prompt MUST include the README content
- AND `max_tokens` MUST equal 300 by default.

### Requirement: Output Passes Through OutputSanitizer (Layer 3)

Both `summary` and `source` MUST be sanitized by
`OutputSanitizer.sanitize_json(...)` before serialization. READMEs
typically contain the highest density of credential-shaped substrings
in the entire index (`.env.example` blocks, deployment instructions,
"how to get an API key" walkthroughs) so the redaction pass is
non-negotiable.

#### Scenario: Generic `api_key=` block in README is redacted

- GIVEN the README contains a setup section with
  `api_key=abc123secret`
- WHEN the LLM summarizes and the tool returns the summary
- THEN the response MUST contain `api_key=[REDACTED]`
- AND a `RedactionIncident` with `pattern=generic` MUST be emitted.

#### Scenario: AWS-shaped key in README is redacted

- GIVEN the README contains `AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE`
- WHEN `summarize_readme` is invoked
- THEN the response MUST contain `AWS_ACCESS_KEY_ID=[REDACTED]`
- AND the audit log MUST emit `event="output.redacted"` with
  `source="summarize_readme"`.

#### Scenario: Clean summary passes through unchanged

- GIVEN the LLM summary contains no matching `SecretPattern`
- WHEN `summarize_readme` is invoked
- THEN the response `summary` MUST equal the LLM output verbatim
- AND `incidents` MUST be empty.

## Error / Edge Cases

- README file missing on disk (declared in manifest but file deleted):
  the use case MUST raise `FileNotFoundError`; the MCP layer MUST
  return a JSON-RPC internal error (`code=-32603`).
- README exceeds the LLM context window: the use case MUST truncate
  to the first 32 KB and MUST emit `audit.warn("llm.truncated", ...)`.
- `display_name` is empty (manifest entry without `display_name`):
  the use case MUST fall back to the project `id` so the result is
  always presentable to the recruiter.
- `--mock-gemini` mode: `LLMPort.summarize` returns the first
  `max_tokens` words verbatim — tests MUST assert on this contract.

## Test Scenarios

| Scenario | Required because |
|---|---|
| README is read for a declared project and listed in `source` | Tool surface contract |
| Unknown `project_id` raises `ManifestProjectNotFoundError` | Input validation |
| `LLMPort.summarize` is called exactly once with the README content | LLM contract |
| Default `max_tokens=300` (tighter than explain_architecture) | Token-budget discipline |
| `api_key=` generic pattern in summary is replaced by `[REDACTED]` | **Layer 3** output sanitization |
| AWS-shaped substring in summary is redacted | **Layer 3** output sanitization |
| GitHub / OpenAI / Gemini / generic patterns are redacted (table-driven) | **Layer 3** output sanitization |
| Clean summary passes through with empty `incidents` | **Layer 3** non-regression |
| Missing README file raises `FileNotFoundError` → JSON-RPC internal error | Defensive default |
| Empty `display_name` falls back to project `id` | Defensive default |
| `--mock-gemini` mode returns deterministic first-`max_tokens`-words summary | Testability |
| `summarize_readme` is registered in the FastMCP server's tool list | MCP mount integration |
