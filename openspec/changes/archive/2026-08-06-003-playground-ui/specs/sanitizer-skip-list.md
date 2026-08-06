# sanitizer-skip-list — Delta Specification

## Purpose

The `OutputSanitizerMiddleware` skip-list at
`src/mcp_server/interfaces/http/middleware/sanitizer.py:39` grows from 2
entries to 6 to accommodate the playground and chat surfaces added by this
change. Per Decision #9 the skip-list stays a module-level tuple literal
(no DB or config-backed routing); adding 4 entries is a single edit plus a
test update.

The middleware's contract is unchanged: it still rewrites every non-skipped
response body through `OutputSanitizer`. What changes is the set of paths
the middleware MUST NOT touch. The two new skips — `/chat*` and
`/playground*` — are required because the middleware buffers entire
response bodies (see `sanitizer.py:86-91`), which would (a) break SSE
delivery (the client would see the entire buffered payload only after the
agent finishes, not as tokens stream) and (b) double-sanitize output that
already passed through `OutputSanitizer.sanitize(...)` inside the use
case.

Layer 3 invariant MUST still hold — the responsibility moves from the
middleware to:

1. `AskPortfolioUseCase.astream` (per-token sanitization — see the
   `agent-streaming` spec).
2. The five non-streaming use cases (`list_projects`, `search_code`,
   `explain_architecture`, `summarize_readme`,
   `get_architecture_diagram`) — each already calls
   `self.sanitizer.sanitize(...)` per their existing `mcp-tools` specs.

## Schema / Interface

```python
# src/mcp_server/interfaces/http/middleware/sanitizer.py:39 — MODIFIED
SKIP_PATH_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/mcp",
    "/chat",          # NEW — chat page
    "/chat/stream",   # NEW — SSE endpoint (would be broken by body buffering)
    "/playground",    # NEW — playground page
    "/playground/api", # NEW — per-tool form endpoints (use case already sanitized)
)
```

## MODIFIED Requirements

### Requirement: Middleware Sanitizes Every Non-Skipped Response Body

The middleware MUST rewrite the response body of every HTTP route through
`OutputSanitizer.sanitize_json` (JSON-shaped payloads) or
`OutputSanitizer.sanitize` (text payloads) before the bytes leave the
server. Routes whose `request.url.path` starts with any prefix in
`SKIP_PATH_PREFIXES` MUST be passed through unchanged.

(Previously: skip-list covered only `/healthz` and `/mcp`. Now extended to
6 prefixes to allow SSE delivery and to avoid double-sanitizing
playground fragments that already passed through the use case's own
sanitizer call.)

#### Scenario: /healthz body is untouched (skip-list member)

- GIVEN `OutputSanitizerMiddleware` is wired
- WHEN a client sends `GET /healthz` whose JSON body happens to contain a
  token-shaped substring in `commit_sha`
- THEN the response body MUST be returned to the client verbatim (the
  middleware MUST NOT inspect or rewrite it).

#### Scenario: /mcp body is untouched (skip-list member)

- GIVEN the FastMCP sub-app is mounted at `/mcp`
- WHEN an MCP client performs an initialize handshake
- THEN the middleware MUST NOT rewrite the response body
- AND the bytes MUST be returned to the MCP client unchanged.

#### Scenario: /playground HTML page is passed through

- GIVEN a recruiter sends `GET /playground`
- WHEN the route renders the index page
- THEN the middleware MUST NOT rewrite the response body
- AND the rendered HTML MUST reach the browser verbatim
- AND `playground/static/htmx.min.js` references MUST remain intact.

#### Scenario: /playground/api/* form fragments are passed through

- GIVEN a recruiter posts `/playground/api/list_projects`
- WHEN the route returns an HTML fragment from the use case
- THEN the middleware MUST NOT rewrite the fragment
- AND the fragment MUST reach the browser verbatim
- AND the use case's own `sanitize(...)` call is the only Layer 3 pass
  applied to its output.

#### Scenario: /chat page is passed through

- GIVEN a recruiter sends `GET /chat`
- WHEN the route renders the chat tab HTML
- THEN the middleware MUST NOT rewrite the response body
- AND the chat script (which references `/chat/stream`) MUST remain
  intact.

#### Scenario: /chat/stream SSE bytes are passed through

- GIVEN a recruiter POSTs `/chat/stream`
- WHEN the agent streams 5 `data:` events and a final `data: [DONE]`
- THEN the middleware MUST NOT buffer or rewrite the SSE body
- AND every `data:` event MUST reach the browser as it is yielded by
  the agent (token-level latency preserved).

#### Scenario: Layer 3 invariant preserved via the use case

- GIVEN the middleware skips `/chat/stream` and `/playground/api/*`
- WHEN the agent emits a token containing `AKIAIOSFODNN7EXAMPLE`
- THEN the `AskPortfolioUseCase.astream` per-token sanitize MUST replace
  the key with `[REDACTED]` before yielding
- AND the SSE event sent over the wire MUST contain `[REDACTED]`
  (no raw secret ever leaves the server).

#### Scenario: Non-skipped routes still pass through sanitizer

- GIVEN any route that is NOT a prefix in `SKIP_PATH_PREFIXES`
- WHEN the route returns a response body containing an AWS-shaped key
- THEN the middleware MUST rewrite the body
- AND a `RedactionIncident` with `pattern=aws` MUST be emitted
  (the middleware contract is unchanged for the rest of the surface).

#### Scenario: Skip-list is a 7-tuple

- GIVEN `sanitizer.py` is imported
- WHEN `SKIP_PATH_PREFIXES` is inspected
- THEN it MUST equal `("/healthz", "/mcp", "/chat", "/chat/stream",
  "/playground", "/playground/api", "/static")` exactly
- AND any test that asserts the tuple contents MUST be updated to match
  the 7-prefix set.

## Error / Edge Cases

- A future route that does NOT start with one of the 7 prefixes (e.g.
  `/admin/debug`) MUST be sanitized by default; the skip-list is
  closed-world and additions require a spec change.
- Adding new prefixes MUST NOT change the middleware's buffering
  behavior for non-skipped routes — the buffer-and-rewrite logic
  applies only to the routes that fall through the skip check.
- The test asserting the skip-list tuple MUST fail loudly if any prefix
  is added without an explicit spec update (closed-world contract).

## Test Scenarios

| Scenario | Required because |
|---|---|
| `SKIP_PATH_PREFIXES` equals the 7-tuple listed in this spec | Closed-world prefix set |
| `OutputSanitizerMiddleware.dispatch` returns the response unchanged for each of the 6 prefixes | Skip-list behavior |
| `_should_skip("/chat/stream", ...)` returns `True`; `_should_skip("/healthcheck", ...)` returns `False` (no false positives) | Prefix matching |
| `AskPortfolioUseCase.astream` per-token sanitize test passes (see `agent-streaming` spec) | Layer 3 invariant under skip |
| Playground form route returns the use case's HTML fragment verbatim (no double-sanitize) | End-to-end playground |
| Non-skipped routes (e.g. a future `/admin`) are still sanitized | Defense-in-depth |
