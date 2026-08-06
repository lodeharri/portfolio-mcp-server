# search_code — Delta Specification

## Purpose

The `search_code` MCP tool runs semantic search over the indexed code
chunks built by the preindex pipeline. A recruiter asks "where did I
implement rate limiting?" and the tool embeds the query, hits
`VectorStorePort.search`, and returns the top-k matches with the chunk
text, file path, line range, and cosine distance.

This tool is the **primary entry point** of the demo. It exercises the full
embedding path (`EmbeddingPort.embed`) and the vector-store round-trip
(`VectorStorePort.search`). All output MUST pass through
`OutputSanitizer` (Layer 3) because matched code chunks are extremely likely
to contain credential-shaped substrings (AWS keys in `auth.py`, GitHub
tokens in `.env.example`, etc.).

The corresponding use case is
`src/mcp_server/application/use_cases/search_code.py::SearchCodeUseCase`.
The FastMCP `@mcp.tool` registration lives in
`src/mcp_server/interfaces/mcp/server.py`.

## Schema / Interface

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

# src/mcp_server/interfaces/mcp/server.py — registration
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

## Requirements

### Requirement: Query Is Embedded Then Searched

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

### Requirement: Optional Project Scope Filter

When the caller passes `project_id`, the use case MUST filter results
client-side (after vector search) to that project only. When `project_id`
is `None`, results MUST span every project in the index.

#### Scenario: project_id filter excludes other projects

- GIVEN the vector store contains 3 chunks in `finance-coach-latam` and
  2 chunks in `landing-page-portfolio` for the same query
- WHEN `search_code(query, project_id="finance-coach-latam")` is invoked
- THEN every result MUST have `project_id == "finance-coach-latam"`.

#### Scenario: No filter returns results across all projects

- GIVEN the same setup as above
- WHEN `search_code(query)` is invoked with no `project_id`
- THEN the response MAY contain results from both projects.

### Requirement: Output Passes Through OutputSanitizer (Layer 3)

Every chunk `content` returned to the MCP client MUST be redacted by
`OutputSanitizer.sanitize(content, source="search_code")` before
serialization. The other fields (`chunk_hash`, `file_path`, line numbers,
`score`, `project_id`) are metadata and MUST pass through unchanged
because they cannot carry secrets.

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

### Requirement: Embedding Errors Surface as Tool Errors

If `EmbeddingPort.embed` raises a `GeminiTransientError` after its
internal retries, the use case MUST propagate it to the MCP layer which
MUST return a JSON-RPC error (`code=-32603` internal error) WITHOUT
leaking the raw exception message (which may contain token-shaped
fragments from the SDK).

#### Scenario: Gemini 429 surfaces as internal error

- GIVEN the Gemini API returns HTTP 429 for 3 consecutive attempts
- WHEN `search_code` is invoked
- THEN the use case MUST raise `GeminiTransientError`
- AND the MCP layer MUST return a sanitized error message
  (`"search temporarily unavailable, retry later"`)
- AND the audit log MUST record `event="tool.error"` with
  `tool="search_code"`.

## Error / Edge Cases

- Empty index (`data/index.sqlite` missing or zero rows): MUST return
  `[]` (not an error). The MCP layer SHOULD log
  `event="search.empty_index"` once per invocation.
- Dim mismatch between query embedding and stored vectors: sqlite-vec
  raises `EmbeddingDimensionMismatchError`; the use case MUST let it
  propagate and the MCP layer MUST surface a 500-class JSON-RPC error.
- Top-k clamped to a maximum of 50 (above which the use case MUST raise
  `ValueError` to prevent resource exhaustion).
- Concurrent invocations: the use case MUST be safe to call from
  multiple MCP requests in parallel (no shared mutable state).

## Test Scenarios

| Scenario | Required because |
|---|---|
| Returns top-k results ordered by ascending `score` | Tool surface contract |
| Empty query raises `ValueError` | Defensive input validation |
| `project_id` filter restricts results to one project | Scope filter |
| AWS-shaped substring in chunk `content` is replaced by `[REDACTED]` | **Layer 3** output sanitization |
| Multiple `SecretPattern` matches in one chunk redacted together | **Layer 3** output sanitization |
| `api_key=value` generic pattern redacted | **Layer 3** output sanitization |
| Clean chunks pass through with empty `incidents` | **Layer 3** non-regression |
| Gemini 429 surfaces as JSON-RPC internal error (no raw exception leak) | **Layer 3** error-boundary sanitization |
| Empty index returns `[]` instead of raising | Defensive default |
| `search_code` is registered in the FastMCP server's tool list | MCP mount integration |
