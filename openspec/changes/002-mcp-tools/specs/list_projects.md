# list_projects — Delta Specification

## Purpose

The `list_projects` MCP tool returns the list of portfolio projects declared
in `config/projects.manifest.yaml` with their display metadata. This is the
"landing page" tool — a recruiter running the demo via MCP Inspector / Claude
Desktop / Cursor asks first "what can I ask about?", and this tool answers.

The tool is read-only and **MUST NOT** call any LLM. It depends solely on the
`ManifestPort` to enumerate projects and `OutputSanitizer` (Layer 3) to
guarantee that secrets never reach the MCP client — even when a project's
`description` contains a token-shaped substring.

The corresponding use case is
`src/mcp_server/application/use_cases/list_projects.py::ListProjectsUseCase`.
The FastMCP `@mcp.tool` registration lives in
`src/mcp_server/interfaces/mcp/server.py`.

## Schema / Interface

```python
# src/mcp_server/application/use_cases/list_projects.py
from dataclasses import dataclass
from mcp_server.application.ports.manifest import ManifestPort, Project

@dataclass(frozen=True)
class ProjectSummary:
    """Project metadata returned to the MCP client."""
    id: str                # stable identifier (e.g. "finance-coach-latam")
    display_name: str      # human-readable name for the playground UI
    description: str       # long-form description (markdown allowed)
    index_chunk_count: int # number of chunks in the index (0 if not indexed)

class ListProjectsUseCase:
    def __init__(
        self,
        *,
        manifest: ManifestPort,
        vector_store: VectorStorePort | None = None,  # optional chunk counts
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
    ) -> None: ...

    def execute(self) -> list[ProjectSummary]: ...

# src/mcp_server/interfaces/mcp/server.py — registration
@mcp.tool(name="list_projects", description="List declared portfolio projects.")
async def list_projects_tool() -> list[dict]:
    """Returns one dict per declared project with id, display_name,
    description, and index_chunk_count. Output is sanitized (Layer 3)."""
```

The MCP tool result is a JSON array of dicts:

```json
[
  {
    "id": "finance-coach-latam",
    "display_name": "Finance Coach LATAM",
    "description": "Personal finance assistant for LATAM users. ...",
    "index_chunk_count": 0
  },
  {
    "id": "landing-page-portfolio",
    "display_name": "Landing Page Portfolio",
    "description": "Personal landing page (Astro + TypeScript). ...",
    "index_chunk_count": 0
  }
]
```

## Requirements

### Requirement: Manifest Is the Only Source

The use case MUST derive the list of projects exclusively from the
configured `ManifestPort`. It MUST NOT walk any filesystem path or parse any
file other than the manifest.

#### Scenario: Returns one entry per declared project

- GIVEN the manifest declares `finance-coach-latam` and `landing-page-portfolio`
- WHEN `list_projects` is invoked
- THEN the response MUST be a JSON array of length 2
- AND each entry MUST include `id`, `display_name`, `description`, and
  `index_chunk_count`.

#### Scenario: Empty manifest returns empty array

- GIVEN a manifest with zero declared projects
- WHEN `list_projects` is invoked
- THEN the response MUST be `[]` (HTTP / JSON-RPC success, not an error)
- AND `audit.warn` MUST NOT be emitted.

### Requirement: Output Passes Through OutputSanitizer (Layer 3)

Every byte returned to the MCP client MUST pass through
`OutputSanitizer.sanitize_json(...)` before serialization. The sanitizer
recursively walks every string field and replaces matches of the five
`SecretPattern` regexes (AWS, GITHUB, OPENAI, GEMINI, GENERIC) with
`[REDACTED]`.

#### Scenario: AWS-shaped substring in description is redacted

- GIVEN a project description contains
  `AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE`
- WHEN `list_projects` is invoked
- THEN the response MUST contain `AWS_ACCESS_KEY_ID=[REDACTED]`
- AND a `RedactionIncident` with `pattern=aws` MUST be recorded by the
  audit logger.

#### Scenario: GitHub personal access token in description is redacted

- GIVEN a project description contains a `ghp_` substring followed by 36
  word characters
- WHEN `list_projects` is invoked
- THEN the token substring MUST be replaced with `[REDACTED]`
- AND the audit log MUST emit `event="output.redacted"` with
  `source="list_projects"`.

#### Scenario: Clean descriptions pass through unchanged

- GIVEN all project descriptions contain no matching `SecretPattern`
- WHEN `list_projects` is invoked
- THEN every `description` string MUST equal the input verbatim
- AND `incidents` MUST be empty.

### Requirement: Chunk Count Is Best-Effort

The `index_chunk_count` field SHOULD report the number of indexed chunks per
project. When the `VectorStorePort` is not wired (e.g. the index DB is
absent or has not been preindexed) the field MUST default to `0` and the
tool MUST still return a valid response.

#### Scenario: Chunk count is zero when no index exists

- GIVEN `data/index.sqlite` does not exist
- WHEN `list_projects` is invoked
- THEN every `index_chunk_count` MUST equal `0`
- AND the response MUST NOT raise an exception.

#### Scenario: Chunk count is positive when index has rows

- GIVEN `data/index.sqlite` contains 42 chunks for `finance-coach-latam`
- WHEN `list_projects` is invoked
- THEN the `finance-coach-latam` entry MUST report `index_chunk_count=42`
- AND `landing-page-portfolio` MUST report its own count independently.

## Error / Edge Cases

- `ManifestPort.load()` raises `ManifestError`: the tool MUST propagate a
  JSON-RPC error with code matching the manifest-failure contract
  (composition root handles the eager `load()` so this is a startup-time
  concern; runtime calls after startup MUST NOT see this error).
- Project entries with `include_subdirs` or `exclude_subdirs` containing
  unusual characters: the tool MUST preserve them verbatim — no path
  normalization is applied to the metadata returned to the client.
- Very large `description` strings (>10 KB): the tool SHOULD return the
  full text; the sanitizer MUST still scan the whole string (no length
  cap) so secrets cannot hide beyond a truncation point.
- Concurrent invocations: `ListProjectsUseCase.execute` MUST be safe to
  call from multiple MCP requests simultaneously — `ManifestPort.projects`
  returns a fresh list per call.

## Test Scenarios

| Scenario | Required because |
|---|---|
| Returns one entry per declared manifest project | Tool surface contract |
| Empty manifest returns `[]` without raising | Defensive default |
| `index_chunk_count` defaults to `0` when index is missing | Best-effort contract |
| AWS-shaped substring in `description` is replaced by `[REDACTED]` | **Layer 3** output sanitization |
| GitHub / OpenAI / Gemini / generic key=value patterns are redacted | **Layer 3** output sanitization |
| Clean descriptions pass through with empty `incidents` | **Layer 3** non-regression |
| `audit.warn("output.redacted", ...)` fires exactly once per redaction | **Layer 5** audit contract |
| `list_projects` is registered in the FastMCP server's tool list | MCP mount integration |
