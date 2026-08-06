# mcp-tools

## Purpose

The six MCP tools that make the server usable from any MCP client (Claude
Desktop, Cursor, MCP Inspector, the future playground chat tab, etc.).
`001-bootstrap` shipped the FastMCP sub-app mount with zero tools; this
spec turns the empty shell into the demo surface. Each tool maps to one
use case in `src/mcp_server/application/use_cases/` and one wrapper in
`src/mcp_server/interfaces/mcp/tools.py` registered with `@mcp.tool`.

All six tools share the same three non-negotiable guarantees from the
project invariants:

- Every byte returned to the MCP client passes through `OutputSanitizer`
  before serialization (Layer 3).
- Every redaction emits exactly one `output.redacted` audit event
  (Layer 5).
- Every domain exception is translated into a JSON-RPC error code via
  the central `translate_tool_error` helper (ADR-002).

The composition root (`src/mcp_server/composition.py`) is the only
module that wires concrete adapters to use cases. The hexagonal invariant
test (`tests/integration/test_hexagonal_invariants.py`) stays GREEN.

---

## Tool 1 — `list_projects`

### Purpose

The `list_projects` MCP tool returns the list of portfolio projects
declared in `config/projects.manifest.yaml` with their display
metadata. This is the "landing page" tool — a recruiter running the demo
via MCP Inspector / Claude Desktop / Cursor asks first "what can I ask
about?", and this tool answers.

The tool is read-only and **MUST NOT** call any LLM. It depends solely on
the `ManifestPort` to enumerate projects and `OutputSanitizer` (Layer 3)
to guarantee that secrets never reach the MCP client — even when a
project's `description` contains a token-shaped substring.

The corresponding use case is
`src/mcp_server/application/use_cases/list_projects.py::ListProjectsUseCase`.

### Schema / Interface

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

# src/mcp_server/interfaces/mcp/tools.py — registration
@mcp.tool(name="list_projects", description="List declared portfolio projects.")
async def list_projects_tool() -> list[dict]:
    """Returns one dict per declared project with id, display_name,
    description, and index_chunk_count. Output is sanitized (Layer 3)."""
```

### Requirements

#### Requirement: Manifest Is the Only Source

The use case MUST derive the list of projects exclusively from the
configured `ManifestPort`. It MUST NOT walk any filesystem path or
parse any file other than the manifest.

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

#### Requirement: Output Passes Through OutputSanitizer (Layer 3)

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

- GIVEN a project description contains a `ghp_` substring followed by
  36 word characters
- WHEN `list_projects` is invoked
- THEN the token substring MUST be replaced with `[REDACTED]`
- AND the audit log MUST emit `event="output.redacted"` with
  `source="list_projects"`.

#### Scenario: Clean descriptions pass through unchanged

- GIVEN all project descriptions contain no matching `SecretPattern`
- WHEN `list_projects` is invoked
- THEN every `description` string MUST equal the input verbatim
- AND `incidents` MUST be empty.

#### Requirement: Chunk Count Is Best-Effort

The `index_chunk_count` field SHOULD report the number of indexed chunks
per project. When the `VectorStorePort` is not wired (e.g. the index DB
is absent or has not been preindexed) the field MUST default to `0` and
the tool MUST still return a valid response.

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

---

## Tool 2 — `search_code`

### Purpose

The `search_code` MCP tool runs semantic search over the indexed code
chunks built by the preindex pipeline. A recruiter asks "where did I
implement rate limiting?" and the tool embeds the query, hits
`VectorStorePort.search`, and returns the top-k matches with the chunk
text, file path, line range, and cosine distance.

This tool is the **primary entry point** of the demo. It exercises the
full embedding path (`EmbeddingPort.embed`) and the vector-store
round-trip (`VectorStorePort.search`). All output MUST pass through
`OutputSanitizer` (Layer 3) because matched code chunks are extremely
likely to contain credential-shaped substrings (AWS keys in `auth.py`,
GitHub tokens in `.env.example`, etc.).

The corresponding use case is
`src/mcp_server/application/use_cases/search_code.py::SearchCodeUseCase`.

### Schema / Interface

```python
# src/mcp_server/application/use_cases/search_code.py
from dataclasses import dataclass
from mcp_server.application.ports.embedding import EmbeddingPort
from mcp_server.application.ports.vector_store import VectorStorePort
from mcp_server.domain.entities import SearchResult

@dataclass(frozen=True)
class SearchCodeRequest:
    query: str                                # natural-language query
    top_k: int = 5                            # max results to return
    project_id: str | None = None             # optional scope filter
    min_score: float = 0.0                    # max acceptable distance

class SearchCodeUseCase:
    def __init__(
        self,
        *,
        embedding: EmbeddingPort,
        vector_store: VectorStorePort,
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
    ) -> None: ...

    def execute(self, request: SearchCodeRequest) -> list[SearchResult]: ...

# src/mcp_server/interfaces/mcp/tools.py — registration
@mcp.tool(name="search_code", description="Semantic search over indexed code chunks.")
async def search_code_tool(
    query: str,
    top_k: int = 5,
    project_id: str | None = None,
) -> list[dict]:
    """Returns up to ``top_k`` matches; each dict has chunk_hash,
    file_path, line_start, line_end, content, score, project_id.
    Output is sanitized (Layer 3)."""
```

### Requirements

#### Requirement: Query Is Embedded Then Searched

The use case MUST call `EmbeddingPort.embed([query])` to obtain a single
query vector, then MUST call
`VectorStorePort.search(query_vector, limit=top_k)` to retrieve the
top-k candidates. It MUST NOT short-circuit embedding or use any
keyword-based fallback.

#### Scenario: Returns top-k results ordered by score

- GIVEN the vector store contains 100 chunks and the query
  `"rate limiting"` embeds to vector `v`
- WHEN `search_code(query="rate limiting", top_k=5)` is invoked
- THEN the response MUST be a JSON array of length ≤ 5
- AND the entries MUST be ordered by ascending `score`
- AND each entry MUST include `chunk_hash`, `file_path`, `line_start`,
  `line_end`, `content`, `score`, and `project_id`.

#### Scenario: Empty query raises a domain error

- GIVEN the caller's `query` argument is `""` or whitespace-only
- WHEN `search_code` is invoked
- THEN the use case MUST raise `ValueError`
- AND the MCP layer MUST translate it into a JSON-RPC error
  (`code=-32602` invalid params).

#### Requirement: Optional Project Scope Filter

When the caller passes `project_id`, the use case MUST filter results
client-side (after vector search) to that project only. When
`project_id` is `None`, results MUST span every project in the index.

#### Scenario: project_id filter excludes other projects

- GIVEN the vector store contains 3 chunks in `finance-coach-latam` and
  2 chunks in `landing-page-portfolio` for the same query
- WHEN `search_code(query, project_id="finance-coach-latam")` is invoked
- THEN every result MUST have `project_id == "finance-coach-latam"`.

#### Scenario: No filter returns results across all projects

- GIVEN the same setup as above
- WHEN `search_code(query)` is invoked with no `project_id`
- THEN the response MAY contain results from both projects.

#### Requirement: Output Passes Through OutputSanitizer (Layer 3)

Every chunk `content` returned to the MCP client MUST be redacted by
`OutputSanitizer.sanitize(content, source="search_code")` before
serialization. The other fields (`chunk_hash`, `file_path`, line
numbers, `score`, `project_id`) are metadata and MUST pass through
unchanged because they cannot carry secrets.

#### Scenario: AWS-shaped key in matched chunk is redacted

- GIVEN a matched chunk contains `AKIAIOSFODNN7EXAMPLE`
- WHEN `search_code` returns the chunk
- THEN the response `content` field MUST contain `[REDACTED]` in place
  of the key
- AND a `RedactionIncident` with `pattern=aws` MUST be emitted.

#### Scenario: Multiple patterns redacted in one chunk

- GIVEN a matched chunk contains both an AWS key and a GitHub PAT
- WHEN `search_code` returns the chunk
- THEN the response MUST contain two `[REDACTED]` placeholders
- AND the audit log MUST emit a single `output.redacted` event with
  `patterns="aws,github"` and `count=2`.

#### Scenario: Generics pattern (`api_key=value`) is redacted

- GIVEN a matched chunk contains `api_key = abc123secret`
- WHEN `search_code` returns the chunk
- THEN the response MUST contain `api_key=[REDACTED]`
- AND a `RedactionIncident` with `pattern=generic` MUST be emitted.

#### Scenario: Clean chunks pass through unchanged

- GIVEN all matched chunks contain no matching `SecretPattern`
- WHEN `search_code` is invoked
- THEN every returned `content` field MUST equal the source verbatim
- AND `incidents` MUST be empty.

#### Requirement: Embedding Errors Surface as Tool Errors

If `EmbeddingPort.embed` raises a `GeminiTransientError` after its
internal retries, the use case MUST propagate it to the MCP layer
which MUST return a JSON-RPC error (`code=-32603` internal error)
WITHOUT leaking the raw exception message (which may contain
token-shaped fragments from the SDK).

#### Scenario: Gemini 429 surfaces as internal error

- GIVEN the Gemini API returns HTTP 429 for 3 consecutive attempts
- WHEN `search_code` is invoked
- THEN the use case MUST raise `GeminiTransientError`
- AND the MCP layer MUST return a sanitized error message
  (`"search temporarily unavailable, retry later"`)
- AND the audit log MUST record `event="tool.error"` with
  `tool="search_code"`.

---

## Tool 3 — `explain_architecture`

### Purpose

The `explain_architecture` MCP tool reads the Architecture Decision
Records (ADRs) for a declared project and returns a Gemini-generated
summary of the project's architectural choices. A recruiter asks
"what's the architecture of finance-coach-latam?" and gets a
synthesized, recruiter-friendly narrative backed by the on-disk ADRs.

The tool reads the ADR file via the path declared in the project's
manifest entry (the `adr_path` extra field that the YAML adapter
preserves on disk) and feeds the text to `LLMPort.summarize`. All
output MUST pass through `OutputSanitizer` (Layer 3) because ADRs
frequently reference third-party services (AWS ARN-shaped strings,
GitHub repo URLs with PAT-like fragments, etc.).

The corresponding use case is
`src/mcp_server/application/use_cases/explain_architecture.py::ExplainArchitectureUseCase`.

### Schema / Interface

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

# src/mcp_server/interfaces/mcp/tools.py — registration
@mcp.tool(name="explain_architecture",
          description="Summarize a project's architecture from its ADRs.")
async def explain_architecture_tool(
    project_id: str, max_tokens: int = 500
) -> dict:
    """Returns {project_id, display_name, summary, sources}.
    Output is sanitized (Layer 3)."""
```

### Requirements

#### Requirement: ADR File Is Read From Manifest Metadata

The use case MUST resolve the ADR file path via the `Project` entry's
`adr_path` extra field (read from `projects[].adr_path` in the
manifest). It MUST NOT walk any filesystem path that is not declared
in the manifest.

#### Scenario: ADRs are read for a declared project

- GIVEN the manifest declares `finance-coach-latam` with
  `adr_path: "openspec/changes/initial-poc/design.md"`
- WHEN `explain_architecture(project_id="finance-coach-latam")` is
  invoked
- THEN the response MUST include
  `sources=[".../initial-poc/design.md"]`
- AND the `summary` MUST be a non-empty string.

#### Scenario: Unknown project_id raises domain error

- GIVEN the caller passes `project_id="nonexistent"`
- WHEN `explain_architecture` is invoked
- THEN the use case MUST raise `ManifestProjectNotFoundError`
- AND the MCP layer MUST return a JSON-RPC error
  (`code=-32602` invalid params).

#### Requirement: LLM Summary Honors the Token Budget

The use case MUST call
`LLMPort.summarize(adr_text, max_tokens=request.max_tokens)` to produce
the summary. The LLM adapter's own retry / backoff policy MUST be
honored — the use case MUST NOT swallow transient failures.

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

#### Requirement: Output Passes Through OutputSanitizer (Layer 3)

Both `summary` and `sources` MUST be sanitized by
`OutputSanitizer.sanitize_json(...)` before serialization. The LLM
summary is the highest-risk surface: it is the only tool output that
is fully model-generated and the model has no awareness of the five
`SecretPattern` regexes.

#### Scenario: AWS ARN-shaped substring in summary is redacted

- GIVEN the LLM summary contains `arn:aws:iam::123456789012:user/admin`
  AND an `AKIAIOSFODNN7EXAMPLE` key
- WHEN `explain_architecture` returns the summary
- THEN the response MUST contain
  `arn:aws:iam::123456789012:user/admin=[REDACTED]`
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

---

## Tool 4 — `summarize_readme`

### Purpose

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

### Schema / Interface

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

# src/mcp_server/interfaces/mcp/tools.py — registration
@mcp.tool(name="summarize_readme",
          description="Summarize a project's README in recruiter-friendly prose.")
async def summarize_readme_tool(
    project_id: str, max_tokens: int = 300
) -> dict:
    """Returns {project_id, display_name, summary, source}.
    Output is sanitized (Layer 3)."""
```

### Requirements

#### Requirement: README Is Read From Manifest Metadata

The use case MUST resolve the README path via the `Project` entry's
`readme_path` extra field (read from `projects[].readme_path` in the
manifest). It MUST NOT walk any filesystem path that is not declared
in the manifest.

#### Scenario: README is read for a declared project

- GIVEN the manifest declares `landing-page-portfolio` with
  `readme_path: "README.md"` (resolved relative to the project root)
- WHEN `summarize_readme(project_id="landing-page-portfolio")` is
  invoked
- THEN the response MUST include `source="<project_root>/README.md"`
- AND the `summary` MUST be a non-empty string.

#### Scenario: Unknown project_id raises domain error

- GIVEN the caller passes `project_id="nonexistent"`
- WHEN `summarize_readme` is invoked
- THEN the use case MUST raise `ManifestProjectNotFoundError`
- AND the MCP layer MUST return a JSON-RPC error
  (`code=-32602` invalid params).

#### Requirement: LLM Summary Honors the Token Budget

The use case MUST call
`LLMPort.summarize(readme_text, max_tokens=request.max_tokens)` to
produce the summary. The default `max_tokens=300` (tighter than
`explain_architecture`'s 500) reflects that READMEs are typically
shorter and recruiter summaries should be punchy.

#### Scenario: LLM is invoked once with the README content

- GIVEN a 10 KB README and `max_tokens=300`
- WHEN `summarize_readme` is invoked
- THEN `LLMPort.summarize` MUST be called exactly once
- AND the prompt MUST include the README content
- AND `max_tokens` MUST equal 300 by default.

#### Requirement: Output Passes Through OutputSanitizer (Layer 3)

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

- GIVEN the README contains
  `AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE`
- WHEN `summarize_readme` is invoked
- THEN the response MUST contain `AWS_ACCESS_KEY_ID=[REDACTED]`
- AND the audit log MUST emit `event="output.redacted"` with
  `source="summarize_readme"`.

#### Scenario: Clean summary passes through unchanged

- GIVEN the LLM summary contains no matching `SecretPattern`
- WHEN `summarize_readme` is invoked
- THEN the response `summary` MUST equal the LLM output verbatim
- AND `incidents` MUST be empty.

#### Scenario: Empty display_name falls back to project id

- GIVEN the manifest entry has no `display_name` (or an empty one)
- WHEN `summarize_readme` returns the result
- THEN `display_name` MUST equal the project `id` so the result is
  always presentable to the recruiter.

---

## Tool 5 — `get_architecture_diagram`

### Purpose

The `get_architecture_diagram` MCP tool returns a project's
architecture diagram as a base64-encoded SVG payload. Recruiters
inspecting a portfolio piece want to see the architecture visually;
sending a raw SVG through the MCP layer would either balloon the
JSON payload or risk the XML being mangled by intermediate JSON-RPC
parsers. Base64 keeps the response deterministic, transport-safe, and
easy for MCP clients (Claude Desktop, Cursor, MCP Inspector) to
render.

The tool is **read-only and LLM-free** — it loads the SVG file and
base64-encodes its bytes. All output MUST pass through
`OutputSanitizer` (Layer 3) because SVG files can carry arbitrary
text content (including `<script>` blocks, comments, and embedded
metadata) that may contain token-shaped substrings.

The corresponding use case is
`src/mcp_server/application/use_cases/get_architecture_diagram.py::GetArchitectureDiagramUseCase`.

### Schema / Interface

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

# src/mcp_server/interfaces/mcp/tools.py — registration
@mcp.tool(name="get_architecture_diagram",
          description="Return a project's architecture diagram as base64-encoded SVG.")
async def get_architecture_diagram_tool(project_id: str) -> dict:
    """Returns {project_id, display_name, media_type, encoding, data, source}.
    Output is sanitized (Layer 3)."""
```

### Requirements

#### Requirement: SVG File Is Read From a Manifest-Declared Path

The use case MUST resolve the SVG path via a manifest-declared extra
field (`diagram_path`, declared per project). It MUST NOT walk any
filesystem path that is not declared in the manifest.

#### Scenario: SVG is returned for a declared project

- GIVEN the manifest declares `finance-coach-latam` with
  `diagram_path: "docs/architecture.svg"`
- WHEN
  `get_architecture_diagram(project_id="finance-coach-latam")` is
  invoked
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

#### Requirement: Base64 Encoding Is Deterministic

The use case MUST base64-encode the SVG bytes using the standard
alphabet (`A–Z a–z 0–9 + /`) without line wrapping. The encoded
string MUST be valid UTF-8 (JSON-safe) so the MCP client can decode
it losslessly.

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

#### Requirement: Output Passes Through OutputSanitizer (Layer 3)

Because SVG is text-bearing XML, every byte returned in `data` MUST
be **decoded → sanitized → re-encoded** through
`OutputSanitizer.sanitize(...)`. This catches credentials embedded as
SVG `<text>` elements, `<script>` blocks, or XML comments.

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

#### Requirement: Non-SVG Files Are Rejected

The use case MUST refuse to base64-encode any file whose content does
not start with `<svg` or `<?xml` after decoding. This protects
against the manifest-declared `diagram_path` pointing to a binary or
non-diagram file by mistake.

#### Scenario: PNG mistakenly declared as diagram raises an error

- GIVEN `diagram_path` resolves to a PNG file
  (magic bytes `89 50 4E 47`)
- WHEN `get_architecture_diagram` is invoked
- THEN the use case MUST raise `ValueError`
- AND the MCP layer MUST return a JSON-RPC internal error
  (`code=-32603`).

#### Requirement: Size Cap Protects the MCP Payload

The use case MUST refuse to base64-encode any SVG larger than 10 MB
after decoding. The MCP payload budget is finite; SVGs beyond this
cap MUST raise `ValueError` (translated to JSON-RPC internal error
`-32603`).

---

## Tool 6 — `ask_portfolio`

### Purpose

The `ask_portfolio` MCP tool is the meta-tool: it exposes a Pydantic
AI `Agent` (backed by Gemini) that has the **other 5 sibling tools**
as function-calling tools. A recruiter asks an open-ended question
("which project is closest to a real production deployment?") and
the agent decides which tools to call (`list_projects`, `search_code`,
`explain_architecture`, `summarize_readme`,
`get_architecture_diagram`) and synthesizes a recruiter-grade answer.

The agent is built inside `composition.compose()` after the 5 sibling
use cases are wired so all 5 are available as function-calling tools.
This is the only MCP tool that uses Pydantic AI; the others are direct
`@mcp.tool` registrations wrapping a single use case.

The corresponding use case is
`src/mcp_server/application/use_cases/ask_portfolio.py::AskPortfolioUseCase`.

### Schema / Interface

```python
# src/mcp_server/application/use_cases/ask_portfolio.py
from dataclasses import dataclass
from pydantic_ai import Agent
from pydantic_ai.usage import UsageLimits

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

# src/mcp_server/interfaces/mcp/tools.py — registration
@mcp.tool(name="ask_portfolio",
          description="Ask a free-form question about the portfolio.")
async def ask_portfolio_tool(
    question: str, conversation_id: str | None = None
) -> dict:
    """Returns {answer, tools_called, conversation_id}.
    Output is sanitized (Layer 3). Rate limited via the application-layer
    RateLimiterPort.check (see "Rate Limiter Caps Blasts" requirement)."""
```

The agent itself is built once in `composition.compose()`:

```python
# src/mcp_server/composition.py — 002-mcp-tools (verified behavior)
from pydantic_ai import Agent

agent = Agent(
    model="google:gemini-2.0-flash",     # pydantic-ai 2.x prefix
    tools=[
        list_projects_tool,              # sibling @mcp.tool functions
        search_code_tool,
        explain_architecture_tool,
        summarize_readme_tool,
        get_architecture_diagram_tool,
    ],
    retries=2,                           # cap Pydantic AI retries
)
# Per-call usage cap is passed to agent.run():
agent.run(
    question,
    usage_limits=UsageLimits(tool_calls_limit=5),
)
```

> **Spec vs. implementation drift (resolved in this main spec):**
> the original delta spec described the agent as
> `Agent(model="google-gla:gemini-2.0-flash", ..., max_tool_calls=5)`.
> The verified implementation uses `model="google:gemini-2.0-flash"`
> (pydantic-ai 2.x renamed `google-gla` → `google`) and applies the
> tool-call cap per-call via
> `usage_limits=UsageLimits(tool_calls_limit=5)` rather than as a
> constructor kwarg. Runtime behavior is identical — the agent aborts
> with `UsageLimitExceeded` after the 5th tool call.

### Requirements

#### Requirement: Agent Registers the Five Sibling Tools

The Pydantic AI `Agent` MUST be initialized with the 5 sibling MCP
tools as function-calling tools. The agent MUST NOT be allowed to
call any tool that is not in this list (no network tools, no shell
tools).

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
- AND an `audit.warn("agent.tool_rejected", tool="Bash")` MUST be
  emitted.

#### Requirement: Multi-Step Latency Is Capped

The agent MUST NOT exceed 5 tool calls per invocation. The cap is
applied per-call via
`agent.run(question, usage_limits=UsageLimits(tool_calls_limit=5))`
inside `AskPortfolioUseCase.execute`. This is the defense against
Pydantic AI multi-step tool-call loops (the `002-mcp-tools` proposal's
risk register).

#### Scenario: Fifth tool call is the last allowed

- GIVEN the agent has already called 4 sibling tools
- WHEN the model requests a 6th tool call
- THEN the agent MUST abort with
  `pydantic_ai.exceptions.UsageLimitExceeded`
- AND the audit log MUST emit
  `event="agent.max_tool_calls_exceeded"`.

#### Scenario: Clean run finishes within budget

- GIVEN the agent resolves the question using 2 tool calls
- WHEN `ask_portfolio` returns
- THEN `tools_called` MUST equal `["list_projects", "search_code"]`
  (or whatever the agent actually invoked).

#### Requirement: Output Passes Through OutputSanitizer (Layer 3)

The agent's final `answer` is fully model-generated and concatenated
from the output of multiple sibling tools. Every byte returned in
`answer` MUST be sanitized via
`OutputSanitizer.sanitize(answer, source="ask_portfolio")` before
serialization. This is the **highest-risk redaction surface** in the
system because it is the only tool that aggregates output from
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

#### Requirement: Rate Limiter Caps Blasts

The use case MUST call `RateLimiterPort.check(client_ip)` before
invoking the agent. When the limiter returns `False`, the use case
MUST raise a domain error that the MCP layer translates to JSON-RPC
error (`code=-32603`) with a sanitized message.

> **Note (Layer 5 implementation reality):** the application-layer
> `RateLimiterPort.check` is the **primary** enforcement for
> `ask_portfolio`. The `security-layers` main spec describes
> `slowapi` as the `/mcp`-endpoint rate limiter; **as of this change
> `slowapi` is NOT yet wired at the `/mcp` endpoint** (see the
> `002-mcp-tools` verify report WARNING W1). The application-layer
> check is the only enforcement today. Wiring `slowapi` on `/mcp` is
> a follow-up action; once it lands, this requirement remains true
> (the application-layer check becomes belt-and-braces).

#### Scenario: 31st request from the same IP is rejected

- GIVEN the same client IP has already made 30 requests in the last
  60 seconds
- WHEN it sends a 31st request
- THEN the use case MUST raise `RateLimitExceeded`
- AND the MCP layer MUST return JSON-RPC internal error
  (`code=-32603`).

#### Requirement: Audit Trail Records Tool Selection

Every tool the agent calls MUST be recorded in the audit log so the
demo recording (the recruiter's screen-share) can later be replayed
to show which sources the agent used.

#### Scenario: Each tool call emits an audit event

- GIVEN the agent calls `list_projects` and then `search_code`
- WHEN `ask_portfolio` returns
- THEN the audit log MUST contain exactly two events:
  `event="agent.tool_call"` with `tool="list_projects"` and
  `event="agent.tool_call"` with `tool="search_code"`.

---

## Cross-Cutting Concerns

### Error Translation — `translate_tool_error`

A central helper in `src/mcp_server/interfaces/mcp/tool_errors.py`
translates domain exceptions into FastMCP `ToolError` instances with
the correct JSON-RPC code. Each of the six `@mcp.tool` wrappers calls
it inside `except DomainError`. The mapping is:

| Exception | JSON-RPC code | Sanitized message |
|---|---|---|
| `ManifestProjectNotFoundError` | `-32602` invalid params | `project_id "<id>" not declared in manifest` |
| `ValueError` (input validation: empty query, top_k > 50, empty question) | `-32602` invalid params | echo the validation message verbatim |
| `ValueError` (output cap: SVG > 10 MB) | `-32603` internal error | `diagram exceeds 10 MB size cap` |
| `FileNotFoundError` (ADR/README/SVG missing) | `-32603` internal error | `referenced file not found` |
| `GeminiTransientError` | `-32603` internal error | `service temporarily unavailable, retry later` |
| `GeminiPermanentError` | `-32603` internal error | `service rejected the request` |
| `RateLimitExceeded` | `-32603` internal error | `rate limit exceeded, retry later` |
| `EmbeddingDimensionMismatchError` | `-32603` internal error | `index dim mismatch — rebuild index` |
| Any other `DomainError` or `McpServerError` | `-32603` internal error | `internal error` |

Programming errors (`TypeError`, `AttributeError`, …) are NOT caught
— they bubble as FastMCP's default 500-class response. See
ADR-002 for the full rationale.

### Per-Tool Sanitization Map

The 5-layer security model mandates Layer 3 sanitization at the
source. Per the ADR-003 coverage table:

| Tool | Sanitize call | `source=` label |
|---|---|---|
| `list_projects` | `sanitize_json(payload, source="list_projects")` | `list_projects` |
| `search_code` | `sanitize(result.content, source="search_code")` per chunk | `search_code` |
| `explain_architecture` | `sanitize(summary, source="explain_architecture")` + `sanitize_json` over result | `explain_architecture` |
| `summarize_readme` | same shape as above | `summarize_readme` |
| `get_architecture_diagram` | `data: bytes → str.decode("utf-8") → sanitize → str.encode → b64encode` | `get_architecture_diagram` |
| `ask_portfolio` | `sanitize(answer, source="ask_portfolio")` on agent's final text | `ask_portfolio` |

### Tool Registration

All six wrappers live in `src/mcp_server/interfaces/mcp/tools.py`
(~10 lines each) and are registered with FastMCP's
`@mcp.tool(name=..., description=...)` decorator at module-import
time. `src/mcp_server/interfaces/mcp/server.py` imports the
`tools` module so the decorators fire at startup.

The Pydantic AI agent (Tool 6) imports the 5 sibling wrappers
**directly** to register them as function-calling tools — same code
path, no duplication (ADR-001). The schemas the agent sees match
exactly what MCP clients see over `/mcp`.

### Manifest `Project` Extras

The `Project` Pydantic entity preserves three extra fields from the
manifest YAML so the file-reader tools can resolve their targets:

| Extra field | Consumer tool | Semantics |
|---|---|---|
| `adr_path` | `explain_architecture` | Path to ADR / design doc for the project |
| `readme_path` | `summarize_readme` | Path to `README.md` |
| `diagram_path` | `get_architecture_diagram` | Path to `architecture.svg` |

The YAML adapter (`infrastructure/adapters/yaml_manifest.py`)
loads these into `Project` extras despite the Pydantic `extra="ignore"`
default. Per `tests/unit/infrastructure/adapters/test_yaml_manifest.py`.

---

## Test Scenarios

| Scenario | Required because |
|---|---|
| 6 tools are listed in `await mcp.list_tools()` (PR1, PR2, PR3 smoke) | MCP mount integration |
| `composition.compose()` builds without `None` placeholders for the new use cases | Hexagonal invariant |
| Hexagonal invariant test stays GREEN (6 invariants) | Architecture invariant |
| `--mock-gemini` mode runs the agent end-to-end and returns `[mock answer to: hi]` | Testability |
| Each tool's output passes through `OutputSanitizer` (5 patterns × 6 tools) | **Layer 3** output sanitization |
| `translate_tool_error` mapping is correct for all known domain exceptions | ADR-002 mapping |
| `audit.warn("agent.tool_call", ...)` fires once per agent tool call | **Layer 5** audit trail |
| `RateLimitExceeded` raised on 31st request from same IP | **Layer 5** rate limit (application-layer) |