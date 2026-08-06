# get_architecture_diagram — Delta Specification

## Purpose

The `get_architecture_diagram` MCP tool returns a project's
architecture diagram as a base64-encoded SVG payload. Recruiters
inspecting a portfolio piece want to see the architecture visually;
sending a raw SVG through the MCP layer would either balloon the JSON
payload or risk the XML being mangled by intermediate JSON-RPC
parsers. Base64 keeps the response deterministic, transport-safe, and
easy for MCP clients (Claude Desktop, Cursor, MCP Inspector) to render.

The tool is **read-only and LLM-free** — it loads the SVG file and
base64-encodes its bytes. All output MUST pass through
`OutputSanitizer` (Layer 3) because SVG files can carry arbitrary
text content (including `<script>` blocks, comments, and embedded
metadata) that may contain token-shaped substrings.

The corresponding use case is
`src/mcp_server/application/use_cases/get_architecture_diagram.py::GetArchitectureDiagramUseCase`.
The FastMCP `@mcp.tool` registration lives in
`src/mcp_server/interfaces/mcp/server.py`.

## Schema / Interface

```python
# src/mcp_server/application/use_cases/get_architecture_diagram.py
from dataclasses import dataclass
from mcp_server.application.ports.manifest import ManifestPort

@dataclass(frozen=True)
class GetArchitectureDiagramRequest:
    project_id: str                # declared project

@dataclass(frozen=True)
class GetArchitectureDiagramResult:
    project_id: str
    display_name: str
    media_type: str                # "image/svg+xml"
    encoding: str                  # "base64"
    data: str                      # base64-encoded SVG bytes
    source: str                    # SVG file path (sanitized)

class GetArchitectureDiagramUseCase:
    def __init__(
        self,
        *,
        manifest: ManifestPort,
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
    ) -> None: ...

    def execute(
        self, request: GetArchitectureDiagramRequest
    ) -> GetArchitectureDiagramResult: ...

# src/mcp_server/interfaces/mcp/server.py — registration
@mcp.tool(name="get_architecture_diagram",
          description="Return a project's architecture diagram as base64-encoded SVG.")
async def get_architecture_diagram_tool(project_id: str) -> dict:
    """Returns {project_id, display_name, media_type, encoding, data, source}.
    Output is sanitized (Layer 3)."""
```

## Requirements

### Requirement: SVG File Is Read From a Manifest-Declared Path

The use case MUST resolve the SVG path via a manifest-declared extra
field (`diagram_path`, declared per project). It MUST NOT walk any
filesystem path that is not declared in the manifest.

#### Scenario: SVG is returned for a declared project

- GIVEN the manifest declares `finance-coach-latam` with
  `diagram_path: "docs/architecture.svg"`
- WHEN `get_architecture_diagram(project_id="finance-coach-latam")` is invoked
- THEN the response MUST include
  `media_type="image/svg+xml"`, `encoding="base64"`
- AND `data` MUST be a non-empty base64 string
- AND `data`, once decoded, MUST start with `<svg` or `<?xml`.

#### Scenario: Unknown project_id raises domain error

- GIVEN the caller passes `project_id="nonexistent"`
- WHEN `get_architecture_diagram` is invoked
- THEN the use case MUST raise `ManifestProjectNotFoundError`
- AND the MCP layer MUST return a JSON-RPC error
  (`code=-32602` invalid params).

### Requirement: Base64 Encoding Is Deterministic

The use case MUST base64-encode the SVG bytes using the standard
alphabet (`A–Z a–z 0–9 + /`) without line wrapping. The encoded string
MUST be valid UTF-8 (JSON-safe) so the MCP client can decode it
losslessly.

#### Scenario: Round-trip is lossless

- GIVEN a 4 KB SVG file
- WHEN `get_architecture_diagram` is invoked
- THEN the bytes `base64.b64decode(response["data"])` MUST equal the
  original file bytes exactly.

#### Scenario: Large SVGs are supported

- GIVEN a 1.5 MB SVG file (well above the 1 MB base64 expansion
  budget)
- WHEN `get_architecture_diagram` is invoked
- THEN the response MUST still be returned (not truncated)
- AND the audit log MUST emit `event="tool.completed"` with
  `bytes=1500000`.

### Requirement: Output Passes Through OutputSanitizer (Layer 3)

Because SVG is text-bearing XML, every byte returned in `data` MUST be
**decoded → sanitized → re-encoded** through `OutputSanitizer.sanitize(...)`.
This catches credentials embedded as SVG `<text>` elements, `<script>`
blocks, or XML comments.

#### Scenario: AWS-shaped key in SVG `<text>` is redacted

- GIVEN the SVG contains
  `<text>AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE</text>`
- WHEN `get_architecture_diagram` is invoked
- THEN the decoded-and-sanitized SVG MUST contain
  `<text>AWS_ACCESS_KEY_ID=[REDACTED]</text>`
- AND the response `data` MUST be the re-encoded sanitized bytes
- AND a `RedactionIncident` with `pattern=aws` MUST be emitted.

#### Scenario: GitHub PAT in SVG `<script>` is redacted

- GIVEN the SVG contains
  `<script>const token = "ghp_..."</script>`
- WHEN `get_architecture_diagram` is invoked
- THEN the decoded-and-sanitized SVG MUST contain `[REDACTED]` in
  place of the token
- AND a `RedactionIncident` with `pattern=github` MUST be emitted.

#### Scenario: Generic `password=...` in SVG comment is redacted

- GIVEN the SVG contains `<!-- password=hunter2 -->`
- WHEN `get_architecture_diagram` is invoked
- THEN the decoded-and-sanitized SVG MUST contain
  `<!-- password=[REDACTED] -->`
- AND a `RedactionIncident` with `pattern=generic` MUST be emitted.

#### Scenario: Clean SVG passes through unchanged

- GIVEN the SVG contains no matching `SecretPattern`
- WHEN `get_architecture_diagram` is invoked
- THEN the decoded `data` MUST equal the original SVG bytes
- AND `incidents` MUST be empty.

### Requirement: Non-SVG Files Are Rejected

The use case MUST refuse to base64-encode any file whose content does
not start with `<svg` or `<?xml` after decoding. This protects against
the manifest-declared `diagram_path` pointing to a binary or
non-diagram file by mistake.

#### Scenario: PNG mistakenly declared as diagram raises an error

- GIVEN `diagram_path` resolves to a PNG file (magic bytes `89 50 4E 47`)
- WHEN `get_architecture_diagram` is invoked
- THEN the use case MUST raise `ValueError`
- AND the MCP layer MUST return a JSON-RPC internal error
  (`code=-32603`).

## Error / Edge Cases

- SVG file missing on disk: MUST raise `FileNotFoundError` → JSON-RPC
  internal error.
- SVG exceeds 10 MB: the use case MUST raise `ValueError` (size cap
  prevents request-budget blowout on the MCP client).
- `--mock-gemini` is irrelevant for this tool — the tool is LLM-free.
- Concurrent invocations: safe (the use case is read-only and
  stateless).
- `display_name` empty: falls back to project `id` (same convention as
  `summarize_readme`).

## Test Scenarios

| Scenario | Required because |
|---|---|
| Returns base64-encoded SVG for a declared project | Tool surface contract |
| Round-trip `base64.b64decode(data) == source bytes` | Encoding contract |
| Unknown `project_id` raises `ManifestProjectNotFoundError` | Input validation |
| AWS-shaped substring in SVG `<text>` is replaced by `[REDACTED]` | **Layer 3** output sanitization |
| GitHub PAT in SVG `<script>` is redacted | **Layer 3** output sanitization |
| `password=...` generic pattern in SVG comment is redacted | **Layer 3** output sanitization |
| Clean SVG passes through with empty `incidents` | **Layer 3** non-regression |
| PNG mistakenly declared as diagram raises `ValueError` | Defensive default |
| Missing SVG file raises `FileNotFoundError` → JSON-RPC internal error | Defensive default |
| SVG > 10 MB raises `ValueError` (size cap) | Defensive default |
| `get_architecture_diagram` is registered in the FastMCP server's tool list | MCP mount integration |
