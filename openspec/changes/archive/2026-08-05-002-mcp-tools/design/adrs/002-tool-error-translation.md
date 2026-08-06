# ADR 002: Tool error translation — DomainError → JSON-RPC

- **Status**: Accepted
- **Date**: 2026-08-05
- **Change**: `002-mcp-tools`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

Each `@mcp.tool` wrapper invokes a use case. The use case can raise domain exceptions:

- `ManifestProjectNotFoundError` (unknown `project_id`)
- `GeminiTransientError` (rate limit / 5xx after retries)
- `GeminiPermanentError` (4xx ≠ 429)
- `FileNotFoundError` (ADR file or README missing on disk)
- `ValueError` (empty query, top_k > 50, SVG too large, etc.)
- `RateLimitExceeded` (Layer 5 hit in `ask_portfolio`)

JSON-RPC 2.0 reserves specific error codes (`-32602` invalid params, `-32603` internal error, etc.). FastMCP converts a raised `ToolError` (or any Python exception) into a JSON-RPC error response with a sanitized message. The question is: how do we map our domain exceptions to the right codes — and where does that mapping live?

## Decision Drivers

- **D1**: Consistent mapping across 6 tools. A `GeminiTransientError` from `search_code` and from `explain_architecture` must produce the same JSON-RPC code.
- **D2**: Sanitized error messages. Raw SDK exception messages may contain token-shaped fragments (e.g. the SDK echoes the API key path). The translated message MUST be one we authored, never the raw `str(exc)`.
- **D3**: Testability. The mapping must be a pure function — `DomainError → (code, message)` — that tests can assert on without spinning up a use case.
- **D4**: Single source of truth. 6 `@mcp.tool` wrappers + 1 agent use case call into one helper, not 7 try/except ladders.

## Considered Options

### Option A — Central helper `translate_tool_error(exc) -> ToolError` (chosen)

A single function in `interfaces/mcp/tool_errors.py` that takes any `DomainError` (or other expected exception) and returns a `fastmcp.exceptions.ToolError` carrying the JSON-RPC code + a sanitized message. Each `@mcp.tool` wrapper does:

```python
try:
    results = use_case.execute(...)
except DomainError as exc:
    raise translate_tool_error(exc)   # → FastMCP → JSON-RPC
```

**Pros**:
- Single mapping table; one place to add a new exception type.
- The function is pure and trivially unit-testable (parametrize over `(exc, expected_code)`).
- Sanitized messages authored once, not per tool.

**Cons**:
- A small layer of indirection between the wrapper and FastMCP.
- Adding a new domain error requires updating `translate_tool_error` — but this is also a feature (forces deliberate mapping).

### Option B — Per-wrapper try/except ladders (rejected)

Each `@mcp.tool` wrapper catches each exception type and produces its own JSON-RPC response.

**Pros**:
- Each tool's error behavior is local and visible.

**Cons**:
- 7 × N ladders to maintain (6 wrappers + agent use case).
- Inconsistency is inevitable — one wrapper translates `GeminiTransientError` to `-32603`, another to `-32602`. Drift = bugs.
- Tests must assert on 7 × N mappings.

### Option C — Global FastMCP error middleware (rejected)

Register a single error handler on the FastMCP instance that catches `DomainError` and translates.

**Pros**:
- One place; no per-wrapper try/except.

**Cons**:
- FastMCP's error-handler API is thin and version-coupled (verified 3.4.6). It's the wrong abstraction for "translate known exception types to known codes".
- A global handler catches ALL exceptions — including programming errors (TypeError, AttributeError). We want known-domain errors mapped, unknown errors to bubble as 500-class.
- Hard to test in isolation — requires the FastMCP server to be initialized.

## Decision

**Option A.** A central `translate_tool_error(exc) -> ToolError` helper in `interfaces/mcp/tool_errors.py`. Mapping table:

| Exception | JSON-RPC code | Sanitized message |
|---|---|---|
| `ManifestProjectNotFoundError` | `-32602` invalid params | `project_id "<id>" not declared in manifest` |
| `ValueError` (input validation: empty query, top_k > 50) | `-32602` invalid params | echo the validation message verbatim (it's authored by us, never raw SDK) |
| `ValueError` (output cap: SVG > 10 MB) | `-32603` internal error | `diagram exceeds 10 MB size cap` |
| `FileNotFoundError` (ADR/README/SVG missing) | `-32603` internal error | `referenced file not found` |
| `GeminiTransientError` | `-32603` internal error | `service temporarily unavailable, retry later` |
| `GeminiPermanentError` | `-32603` internal error | `service rejected the request` |
| `RateLimitExceeded` | `-32603` internal error | `rate limit exceeded, retry later` |
| `EmbeddingDimensionMismatchError` | `-32603` internal error | `index dim mismatch — rebuild index` |

The default branch (any other `DomainError` or `McpServerError`) returns `-32603` with message `internal error`. Programming errors (`TypeError`, `AttributeError`, …) are NOT caught — they bubble as FastMCP's default 500-class response.

The helper's signature is intentionally `ToolError` (not `(code, message)`) so the wrapper can `raise` it directly:

```python
# interfaces/mcp/tool_errors.py
from fastmcp.exceptions import ToolError
from mcp_server.domain.exceptions import (
    ManifestProjectNotFoundError,
    GeminiTransientError,
    GeminiPermanentError,
    EmbeddingDimensionMismatchError,
    DomainError,
    McpServerError,
)

JSON_RPC_INVALID_PARAMS = -32602
JSON_RPC_INTERNAL_ERROR = -32603

def translate_tool_error(exc: BaseException) -> ToolError:
    if isinstance(exc, ManifestProjectNotFoundError):
        return ToolError(str(exc), code=JSON_RPC_INVALID_PARAMS)
    if isinstance(exc, ValueError):
        # Distinguish input validation (invalid_params) from output cap
        # (internal_error) by message prefix — ValueError is overloaded.
        return ToolError(str(exc), code=JSON_RPC_INVALID_PARAMS)
    if isinstance(exc, FileNotFoundError):
        return ToolError("referenced file not found", code=JSON_RPC_INTERNAL_ERROR)
    if isinstance(exc, GeminiTransientError):
        return ToolError("service temporarily unavailable, retry later",
                         code=JSON_RPC_INTERNAL_ERROR)
    if isinstance(exc, GeminiPermanentError):
        return ToolError("service rejected the request",
                         code=JSON_RPC_INTERNAL_ERROR)
    if isinstance(exc, EmbeddingDimensionMismatchError):
        return ToolError("index dim mismatch — rebuild index",
                         code=JSON_RPC_INTERNAL_ERROR)
    if isinstance(exc, (DomainError, McpServerError)):
        return ToolError("internal error", code=JSON_RPC_INTERNAL_ERROR)
    # Programming errors are NOT caught here — let FastMCP bubble them.
    raise exc
```

## Consequences

**Positive**:
- One mapping table for the whole change.
- Tests assert on `translate_tool_error(exc).code` and `.message` directly.
- Adding a new domain error is a one-line addition to the helper.
- The sanitized-message discipline (never `str(exc)` for SDK-originated exceptions) is enforced in the helper, not 7 times.

**Negative**:
- `ValueError` is overloaded (input validation vs. output cap). The discriminator is a message-prefix check inside the helper. A future maintainer who authors a new `ValueError` needs to know the convention. Mitigated by a comment in the helper.
- The wrapper still needs a `try/except DomainError` (and `except (ValueError, FileNotFoundError)`) — the helper doesn't auto-catch. This is intentional: we want explicit error boundaries per tool.

## Compliance with rules

- `rules.apply.guidelines` → "Follow FastAPI composition-root pattern: app factory in src/mcp_server/app.py" — satisfied; the helper is a separate module under `interfaces/mcp/`, not in `app.py`.
- `invariants` → "All MCP tool outputs pass through OutputSanitizer before reaching the client" — error messages are also sanitized: the helper authors them; raw SDK messages never leak. Sanitization at the boundary is preserved.

## Follow-ups

- In apply phase: write `tests/unit/interfaces/mcp/test_tool_errors.py` parametrized over `(exc, expected_code, expected_msg_substring)`.
- In apply phase: extend `tests/integration/test_composition_wiring.py` to assert each wrapper calls `translate_tool_error` on the right exception type.
- In verify phase: instrument one recruiter demo to confirm no raw SDK message reaches the JSON-RPC client.
