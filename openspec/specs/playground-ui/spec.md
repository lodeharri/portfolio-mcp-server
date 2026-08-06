# playground-ui

## Purpose

The browser-facing surface that exposes the six MCP tools to recruiters
without Claude Desktop, Cursor, or MCP Inspector. `001-bootstrap`
shipped the FastAPI factory; `002-mcp-tools` shipped the six MCP tools;
this change adds a **parallel HTTP surface** (`/`, `/playground`, `/chat`)
that reuses the same use cases from `composition.py` without moving
business logic and without adding new ports or adapters.

The MCP transport at `/mcp` remains the canonical interface; the
playground is a parallel adapter package (`interfaces/http/web/`) for
human visitors. A recruiter lands on `/`, clicks into `/playground` or
`/chat`, and exercises the same five non-agent tools plus the streaming
`ask_portfolio` variant.

Three design decisions make this possible without violating the
hexagonal rules:

1. **HTMX 1.9.10 vendored** at `playground/static/htmx.min.js` (47,755
   bytes / ~48 KB uncompressed; ~14 KB gzipped; no CDN, no network on
   page load). The five form endpoints return HTML fragments HTMX swaps
   into a per-tool `<div>`. (Decision #1, ADR-001.)
2. **Stateful client + stateless server** for chat persistence — the
   browser owns the conversation in `localStorage`; the server holds no
   chat state. (Decision #11, ADR-004.)
3. **Per-token sanitization in `AskPortfolioUseCase.astream`** — the
   HTTP middleware cannot catch SSE bytes (it buffers full bodies), so
   the use case owns the Layer 3 redaction per chunk. The middleware's
   skip-list is extended to 7 prefixes to allow SSE flow. (Decision #6,
   Decision #9, ADR-005.)

The new surface lives in `src/mcp_server/interfaces/http/web/`
(`router.py`, `playground.py`, `chat.py`, `deps.py`, `templates.py`,
`__init__.py`). The router is mounted in `app.py` between
`build_healthz_router()` and `app.mount("/mcp", mcp_app)` — same slot as
the existing routers; `app-bootstrap` spec stays correct because the
spec describes the factory + lifecycle, not the per-route list.

---

## Browser Surface

### Requirement: Landing Page at GET /

`GET /` MUST render a server-rendered HTML page that introduces the demo
and links to `/playground` and `/chat`. The page MUST call
`list_projects` from `request.app.state.composition` so the rendered HTML
contains the same project list a recruiter would see via MCP.

#### Scenario: Landing page returns 200 with project list

- GIVEN the manifest declares two projects
- WHEN a browser sends `GET /`
- THEN the response MUST be 200 with `Content-Type: text/html`
- AND the body MUST contain one anchor per declared `project.id`
- AND each anchor MUST link to `/playground`.

#### Scenario: Landing page renders without a 500 when no index exists

- GIVEN `data/index.sqlite` is missing (chunk counts default to 0)
- WHEN a browser sends `GET /`
- THEN the response MUST still be 200
- AND every project's `index_chunk_count` MUST render as `0`.

### Requirement: Playground Index at GET /playground

`GET /playground` MUST render five form cards — one per non-agent MCP
tool (`list_projects`, `search_code`, `explain_architecture`,
`summarize_readme`, `get_architecture_diagram`). Each card MUST submit
to its dedicated `POST /playground/api/{tool_name}` endpoint with
`hx-post` / `hx-target` / `hx-swap` attributes so HTMX swaps the response
into a per-card result `<div>` without a full page reload.

#### Scenario: Playground page renders 5 form cards

- GIVEN the server is running with the standard composition
- WHEN a browser sends `GET /playground`
- THEN the response MUST be 200 with `Content-Type: text/html`
- AND the body MUST contain exactly 5 `<form>` elements
- AND each form MUST have `hx-post` targeting its
  `/playground/api/{tool_name}` endpoint.

#### Scenario: HTMX and CSS assets are referenced

- GIVEN the server is running
- WHEN `GET /playground` is rendered
- THEN the HTML MUST reference `/static/htmx.min.js`
- AND MUST reference `/static/style.css`
- AND MUST NOT reference any external CDN URL for HTMX.

### Requirement: Per-Tool Form Fragments at POST /playground/api/{tool_name}

For each of the five non-agent tools, `POST /playground/api/{tool_name}`
MUST accept `application/x-www-form-urlencoded` parameters, invoke the
matching use case from `request.app.state.composition`, and return an
HTML fragment (not a full page). The use case's own
`OutputSanitizer.sanitize(...)` call is the only Layer 3 pass required —
the middleware skip-list (see `security-layers` § Middleware skip-list)
ensures no double-sanitization.

The five endpoints MUST be:
`list_projects`, `search_code`, `explain_architecture`,
`summarize_readme`, `get_architecture_diagram`.

#### Scenario: list_projects fragment returns rendered project list

- GIVEN the manifest declares two projects
- WHEN the browser POSTs `/playground/api/list_projects` with no body
- THEN the response MUST be 200 with `Content-Type: text/html`
- AND the body MUST be a Jinja2 fragment (no `<html>` or `<body>` wrapper)
- AND the fragment MUST contain both project ids.

#### Scenario: search_code fragment renders matched chunks

- GIVEN the use case returns three matches for `query="rate limit"`
- WHEN the browser POSTs `/playground/api/search_code` with
  `query=rate+limit`
- THEN the response MUST be 200 with `Content-Type: text/html`
- AND the fragment MUST contain three `<article>` (or equivalent) blocks
  each showing `file_path` and the sanitized `content`.

#### Scenario: explain_architecture surfaces summary + sources

- GIVEN the manifest declares `finance-coach-latam` with an `adr_path`
- WHEN the browser POSTs `/playground/api/explain_architecture` with
  `project_id=finance-coach-latam`
- THEN the response MUST be 200 with `Content-Type: text/html`
- AND the fragment MUST include the project `display_name`
- AND MUST include a non-empty `summary` paragraph
- AND MUST list `sources` as anchor tags.

#### Scenario: summarize_readme renders one-paragraph summary

- GIVEN the manifest declares `landing-page-portfolio` with a `readme_path`
- WHEN the browser POSTs `/playground/api/summarize_readme` with
  `project_id=landing-page-portfolio`
- THEN the response MUST be 200 with `Content-Type: text/html`
- AND the fragment MUST include the project `display_name`
- AND MUST include a one-paragraph `summary` block
- AND MUST include a `source` link to the README.

#### Scenario: get_architecture_diagram renders inline SVG

- GIVEN the manifest declares `finance-coach-latam` with a `diagram_path`
- WHEN the browser POSTs `/playground/api/get_architecture_diagram` with
  `project_id=finance-coach-latam`
- THEN the response MUST be 200 with `Content-Type: text/html`
- AND the fragment MUST contain an inline `<svg>` element
- AND MUST include a "view full diagram" link to the raw SVG.

#### Scenario: Unknown project_id surfaces a 4xx with a fragment

- GIVEN the caller posts `project_id=nonexistent` to
  `POST /playground/api/explain_architecture`
- WHEN the request reaches the use case
- THEN the route MUST return a 4xx response (per the MCP tool's contract
  for `ManifestProjectNotFoundError`)
- AND the body MUST be an HTML fragment explaining the failure (not raw
  JSON).

### Requirement: Chat Page at GET /chat

`GET /chat` MUST render the chat tab UI. The template MUST include an
inline `<script>` (or `chat.js` static file) that owns the conversation
state in `localStorage` per Decision #11. The script MUST issue a
`fetch` (with `ReadableStream` parsing) against `/chat/stream` because
POSTing the full messages array is required (native `EventSource` only
supports GET), and MUST append each `data:` chunk to the visible
transcript. HTMX is used alongside for the non-chat cards but NOT for
the chat stream.

#### Scenario: Chat page returns 200 with SSE client wired

- GIVEN the server is running
- WHEN a browser sends `GET /chat`
- THEN the response MUST be 200 with `Content-Type: text/html`
- AND the HTML MUST include a `<script>` block that POSTs to
  `/chat/stream`
- AND MUST reference `/static/htmx.min.js` (HTMX can be used alongside
  the custom SSE client for the non-chat cards).

### Requirement: Streaming Endpoint at POST /chat/stream

`POST /chat/stream` MUST accept a JSON body of the form
`{"messages": [{"role": "user", "content": "..."}, ...]}`,
invoke `AskPortfolioUseCase.astream` (see § Agent Streaming Variant),
and return an `EventSourceResponse` whose body is a stream of SSE events.
Each token chunk MUST be emitted as one SSE event
`data: <token>\n\n`, terminated by `data: [DONE]\n\n` when the agent
finishes. A mid-stream exception MUST yield `data: [ERROR]\n\n` as the
terminal event (REL-3 amendment).

#### Scenario: Chat stream delivers at least 2 chunks within 5 seconds

- GIVEN the agent mock mode is active (no `GEMINI_API_KEY`)
- WHEN the browser POSTs `/chat/stream` with a single user message
- THEN the response MUST be 200 with `Content-Type: text/event-stream`
- AND the body MUST contain at least 2 `data:` events within 5 seconds
- AND the final event MUST be `data: [DONE]\n\n`.

#### Scenario: Chat stream sends full messages array per request

- GIVEN the browser holds a 3-turn conversation in `localStorage`
- WHEN the browser POSTs `/chat/stream`
- THEN the request body MUST contain `messages` with length ≥ 3
- AND each entry MUST include both `role` and `content`.

#### Scenario: Chat stream reuses the existing AskPortfolioUseCase

- GIVEN the composition root has wired `AskPortfolioUseCase`
- WHEN `/chat/stream` is invoked
- THEN the route MUST read the use case from
  `request.app.state.composition`
- AND MUST call its `astream` method (not `execute`).

#### Scenario: Chat stream emits [ERROR] on mid-stream exception (REL-3)

- GIVEN the underlying agent raises mid-stream
- WHEN `/chat/stream` is invoked
- THEN the SSE body MUST end with `data: [ERROR]\n\n` (REL-3)
- AND the browser MUST NOT append any partial assistant text to
  `localStorage` (per § Client-Side Persistence).

### Requirement: Static Assets Served With Cache-Control

HTMX 1.9.10 MUST be vendored at `playground/static/htmx.min.js`
(Decision #1, no CDN; 47,755 bytes / ~48 KB uncompressed per REL-6).
Templates MUST live in `playground/templates/`. Static files MUST be
served by Starlette's `StaticFiles` with
`Cache-Control: public, max-age=31536000, immutable` so the browser
caches the vendored HTMX across page loads. The middleware skip-list
includes `/static` so the regex redaction does not corrupt JS bytes.

#### Scenario: /static/htmx.min.js is reachable from the image

- GIVEN the runtime container is built per `container-image` § Playground COPY
- WHEN a browser sends `GET /static/htmx.min.js`
- THEN the response MUST be 200
- AND the body MUST contain the literal string `1.9.10` (the version
  marker embedded in the minified HTMX 1.9.10 file; the upstream
  `/* htmx.org */` banner is stripped in the production min build)
- AND the body MUST be > 10 KB
- AND the response MUST include
  `Cache-Control: public, max-age=31536000, immutable`.

#### Scenario: Templates are rendered through a single Jinja2 environment

- GIVEN the server is running
- WHEN any `GET /`, `GET /playground`, or `GET /chat` route is invoked
- THEN the response MUST be rendered by the shared `Jinja2Templates`
  instance bound to `playground/templates/`
- AND the response MUST extend `playground/templates/base.html`.

### Requirement: Solarized Phosphor Palette

`playground/static/style.css` MUST define the Solarized Phosphor palette
via CSS custom properties under `:root` (Decision #11 reference:
`https://ethanschoonover.com/solarized/`). The palette MUST include the
eight base tones (`--solar-base03` … `--solar-base3`) and the eight
accent colors (`--solar-yellow`, `--solar-orange`, `--solar-red`,
`--solar-magenta`, `--solar-violet`, `--solar-blue`, `--solar-cyan`,
`--solar-green`).

#### Scenario: Palette tokens are present in style.css

- GIVEN the static asset bundle is built
- WHEN a browser fetches `/static/style.css`
- THEN the response MUST contain `:root { ... }` declarations for all 16
  palette tokens
- AND the hex values MUST match the canonical Solarized Phosphor set
  (`#002b36`, `#073642`, `#586e75`, `#657b83`, `#839496`, `#93a1a1`,
  `#eee8d5`, `#fdf6e3`, plus `#b58900`, `#cb4b16`, `#dc322f`, `#d33682`,
  `#6c71c4`, `#268bd2`, `#2aa198`, `#859900`).

---

## Stateless-Server + Stateful-Client Persistence Model

### Requirement: Server Holds No Chat State

The server MUST NOT persist conversation history for `/chat` requests in
any form. There MUST be no database table, no in-memory map keyed by
session, no cookie, and no IP-based key derived from
`request.client.host`. The server is allowed to keep the in-process
rate-limit map (slowapi, Layer 5) because that is request-shaping, not
conversation-state.

#### Scenario: Server emits no persistence on /chat/stream

- GIVEN the same client IP sends 5 consecutive `/chat/stream` requests
- WHEN each request completes
- THEN the server MUST NOT have written any new rows to any DB
- AND MUST NOT have created any new module-level state keyed by the
  client IP or any session id
- AND a process restart MUST cause the next request to behave
  identically (server has amnesia between requests).

#### Scenario: Rate-limit state is the only per-IP server memory

- GIVEN slowapi is wired (Layer 5)
- WHEN the 31st `/chat/stream` request arrives from the same IP within
  60 seconds
- THEN the response MUST be HTTP 429 (the request-shaping state is
  allowed)
- AND the server MUST NOT store any conversation content keyed by that IP.

### Requirement: Client Persists History in localStorage

The chat UI MUST persist the full conversation history in the browser's
`localStorage` under a key namespaced by a random per-session UUID. The
session UUID MUST be generated via `window.crypto.randomUUID()` on the
recruiter's first visit (no PII, no IP derivation, no cookie consent
gate).

#### Scenario: Session UUID is generated on first visit

- GIVEN the recruiter opens `/chat` for the first time in a fresh browser
  profile (no `mcp-playground-chat:*` keys present)
- WHEN the chat script runs
- THEN it MUST call `window.crypto.randomUUID()`
- AND MUST store the result under the key `mcp-playground-chat:sid`
- AND MUST use that UUID as the namespace prefix for the history key.

#### Scenario: History is restored on page reload

- GIVEN the recruiter reloads `/chat` after sending two messages
- WHEN the chat script runs
- THEN it MUST read the stored history
- AND MUST render every prior turn in the visible transcript BEFORE
  accepting new input.

#### Scenario: History key is namespaced by session UUID

- GIVEN two browser profiles generate different session UUIDs
- WHEN each profile writes its own history
- THEN the localStorage keys MUST differ (the UUID is part of the key)
- AND reading one profile's history MUST NOT leak into the other.

### Requirement: Full Messages Sent Per Request

Every `POST /chat/stream` request from the client MUST include the
entire conversation history as a JSON body. The server MUST NOT be
expected to reconstruct prior turns from any prior state — it MUST
treat the `messages` array as the only source of context.

#### Scenario: First turn sends a length-1 messages array

- GIVEN the recruiter has not chatted before (history is empty)
- WHEN the recruiter sends their first message
- THEN the POST body MUST contain
  `{"messages": [{"role": "user", "content": "..."}]}`
- AND `messages.length` MUST equal 1.

#### Scenario: Nth turn sends a length-N messages array

- GIVEN the recruiter has sent 3 prior turns (history has 6 entries:
  user, assistant, user, assistant, user, assistant)
- WHEN the recruiter sends a 4th user message
- THEN the POST body MUST contain `messages` with length 7 (6 prior + 1
  new user)
- AND every prior turn MUST appear with its original `role` and `content`.

#### Scenario: Server response carries only the new assistant message

- GIVEN the agent finishes and yields a 50-token reply
- WHEN the SSE stream terminates with `data: [DONE]\n\n`
- THEN the body MUST contain exactly one assistant message worth of
  tokens (concatenated from the SSE events)
- AND MUST NOT echo the prior turns back to the client.

### Requirement: Assistant Reply Appended on DONE Event

The client MUST accumulate token chunks until it receives the
`data: [DONE]\n\n` sentinel, then MUST append a single
`{role: "assistant", content: <accumulated>}` entry to the stored
history and to the visible transcript. If the connection drops before
`data: [DONE]` is received, the client MUST NOT append a partial
assistant message.

#### Scenario: DONE event triggers a single append

- GIVEN the agent streams 5 tokens and then `data: [DONE]`
- WHEN the fetch + ReadableStream reader sees the DONE event
- THEN the client MUST append exactly one entry to `localStorage`
- AND the entry MUST have `role: "assistant"` and the full concatenated
  content.

#### Scenario: Connection drop before DONE does not append

- GIVEN the network drops after 3 of 5 tokens
- WHEN no DONE event arrives within the read timeout
- THEN the client MUST NOT modify `localStorage`
- AND MUST render an inline "connection lost, retry?" affordance (no
  partial assistant message in storage).

### Requirement: Graceful Degradation When localStorage Is Unavailable

When `localStorage` is unavailable (private browsing mode, quota
exceeded, or browser policy block), the chat MUST continue to function
as a single-message-at-a-time tool. The UI MUST show a one-line notice
explaining that the conversation will not persist across reloads, and
MUST NOT crash.

#### Scenario: localStorage throws on write

- GIVEN the browser blocks `localStorage.setItem` (private mode or quota)
- WHEN the chat script tries to save the conversation
- THEN it MUST catch the exception
- AND MUST NOT propagate the error to the user as an uncaught exception
- AND MUST display the notice "Conversations in this browser are not
  saved between reloads."

#### Scenario: Single-message mode still works without persistence

- GIVEN `localStorage` is unavailable
- WHEN the recruiter sends a message
- THEN the client MUST POST only the current user message (no prior
  turns) to `/chat/stream`
- AND the assistant reply MUST render in the transcript
- AND a page reload MUST leave the transcript empty (no prior turns).

### Requirement: No Tracking, No Cookies, No IP-Based Keys

The chat surface MUST NOT use cookies, MUST NOT fingerprint the browser
beyond the existing session UUID in `localStorage`, and MUST NOT use
`request.client.host` for any session-shaping logic. The only client-
side identifier is the `localStorage` UUID; the server does not see it.

#### Scenario: No Set-Cookie header on /chat/stream

- GIVEN any `/chat/stream` request
- WHEN the server responds
- THEN the response MUST NOT include a `Set-Cookie` header
- AND MUST NOT include any tracking pixel, beacon, or analytics ping.

#### Scenario: Server-side logs do not contain the session UUID

- GIVEN the recruiter sends `/chat/stream` requests
- WHEN the audit log is inspected
- THEN no log line MUST contain the `localStorage` UUID value
- AND no log line MUST contain the full message content (only the event
  type and a redacted length).

---

## Agent Streaming Variant

This section is the streaming sibling of `mcp-tools` Tool 6
(`ask_portfolio`). The MCP buffered path (`/mcp`) keeps using
`aexecute` (final answer only); `astream` is purely additive for the
browser playground's `/chat/stream` route. See `mcp-tools` spec for the
non-streaming contract.

### Requirement: AgentPort.stream Returns AgentChunk Stream

`AgentPort` MUST declare
`async def stream(request: AgentRequest, tools: list[Any]) -> AsyncIterator[AgentChunk]`.
`AgentChunk` MUST be a Pydantic model with
`kind: Literal["token", "tool_call", "done", "error"]` (REL-3 amendment
to the closed set — added in PR2a for mid-stream exception handling)
and `data: str | dict`. The `"error"` kind carries the stringified
exception in `data` and is translated by the SSE layer to a terminal
`data: [ERROR]\n\n` event so the client can render an inline retry
affordance. The method MUST be implemented by both
`LangChainAgentAdapter` and `_MockLangChainAgentAdapter`.

#### Scenario: Both adapters implement stream

- GIVEN the composition root has selected either the real or mock adapter
- WHEN `adapter.stream(AgentRequest(...), tools)` is awaited
- THEN it MUST return an async iterator of `AgentChunk` instances
- AND every yielded object MUST have a valid `kind` value
  (`"token"`, `"tool_call"`, `"done"`, or `"error"` per the closed set
  extended by REL-3).

#### Scenario: Stream is additive (run still works)

- GIVEN the composition root has wired the agent
- WHEN `adapter.run(...)` is invoked
- THEN it MUST return an `AgentResponse` exactly as before
- AND MUST NOT depend on `stream` being implemented.

### Requirement: LangChainAgentAdapter.stream Uses stream_mode="messages"

`LangChainAgentAdapter.stream` MUST invoke
`agent.astream(input, config, stream_mode="messages")` and MUST yield an
`AgentChunk(kind="token", data=...)` for each `AIMessageChunk` event. It
MUST ignore non-AI message types (HumanMessage, ToolMessage, etc.) so
the client only sees model tokens.

`langgraph>=0.2,<2.0` is pinned in `pyproject.toml` to keep the
streaming event surface stable.

#### Scenario: AI tokens are yielded

- GIVEN a Gemini-backed `LangChainAgentAdapter`
- WHEN `stream(...)` is called with a user question
- THEN it MUST yield exactly one
  `AgentChunk(kind="token", data=<chunk>)` per `AIMessageChunk` from the
  agent
- AND MUST skip non-AI messages (no `AgentChunk` for tool-only messages).

#### Scenario: stream_mode="messages" is the only mode used

- GIVEN any invocation of `LangChainAgentAdapter.stream`
- WHEN the underlying LangGraph call is inspected
- THEN the `stream_mode` kwarg MUST equal `"messages"`
- AND the recursion limit MUST equal
  `request.max_tool_calls * 2 + 1`.

### Requirement: Mock Agent Streams 5 Tokens + DONE

`_MockLangChainAgentAdapter.stream` MUST yield exactly 5 token chunks
(`"Tok"`, `"en"`, `"ized"`, `" mock"`, `" answer"`), each separated by
`asyncio.sleep(0.05)` to simulate network latency, followed by one
`AgentChunk(kind="done", data="")`.

#### Scenario: Mock stream yields 5 tokens + DONE

- GIVEN the mock agent is active (no `GEMINI_API_KEY`)
- WHEN `stream(...)` is awaited
- THEN it MUST yield exactly 5 `AgentChunk(kind="token", ...)` events
- AND the tokens MUST equal
  `("Tok", "en", "ized", " mock", " answer")` in that order
- AND a final `AgentChunk(kind="done", data="")` MUST be yielded.

### Requirement: AskPortfolioUseCase.astream Enforces the Same Rate-Limit Gate

`AskPortfolioUseCase.astream` MUST call
`self.rate_limiter.check(request.client_ip)` exactly once per request
before iterating the agent's stream (same gate as `aexecute`). When the
limiter returns `False`, the method MUST raise `RateLimitExceeded`
without invoking the agent.

#### Scenario: Rate-limit gate fires once per request

- GIVEN a fresh client IP
- WHEN `astream(request)` is called for the first time
- THEN `self.rate_limiter.check(request.client_ip)` MUST be called
  exactly once
- AND on the 31st call within 60 s from the same IP, the method MUST
  raise `RateLimitExceeded`
- AND no agent call MUST occur after the gate fails.

### Requirement: AskPortfolioUseCase.astream Sanitizes Per Token (Layer 3)

`AskPortfolioUseCase.astream` MUST call
`self.sanitizer.sanitize(token, source="ask_portfolio")` on EACH token
chunk BEFORE yielding the chunk to the caller. The Layer 3 invariant
holds per-chunk — the middleware can no longer catch SSE bytes (see
`security-layers` § Middleware skip-list; `OutputSanitizerMiddleware`
buffers full bodies).

#### Scenario: Token containing AWS-shaped key is redacted

- GIVEN the agent emits a token `AKIAIOSFODNN7EXAMPLE` mid-stream
- WHEN `astream` yields the token chunk
- THEN the chunk's `answer_token` MUST contain `[REDACTED]` in place of
  the key
- AND a `RedactionIncident` with `pattern=aws` MUST be emitted to the
  audit log.

#### Scenario: Clean tokens pass through unchanged

- GIVEN the agent emits a token `Recruiter-friendly answer text`
- WHEN `astream` yields the token chunk
- THEN the chunk's `answer_token` MUST equal the input verbatim
- AND no `RedactionIncident` MUST be emitted for that token.

### Requirement: AskPortfolioUseCase.astream Emits Tool-Call Audit Events

When the agent invokes a sibling tool, `astream` MUST emit the same
`audit.info("agent.tool_call", tool=<name>, source="ask_portfolio")`
event that `aexecute` emits. The event MUST be fired exactly once per
tool invocation, mirroring `aexecute`'s contract.

### Requirement: AskPortfolioUseCase.astream Terminates With Final Result

`astream` MUST yield a final `AskPortfolioChunk(kind="done", result=...)`
when the agent finishes. The `result` MUST be an `AskPortfolioResult`
whose `answer` is the sanitized concatenation of all token chunks, with
`tools_called` populated and `conversation_id` echoed from the request.

#### Scenario: DONE chunk carries sanitized final result

- GIVEN the agent emits 5 tokens spelling "Tokenized mock answer" and
  invokes one tool
- WHEN `astream` finishes
- THEN the final chunk MUST be
  `AskPortfolioChunk(kind="done", result=AskPortfolioResult(
  answer="Tokenized mock answer", tools_called=["list_projects"],
  conversation_id=None))`
- AND `result.answer` MUST equal the concatenation of the sanitized
  tokens.

### Requirement: AskPortfolioUseCase.astream Yields an ERROR Chunk on Mid-Stream Exception (REL-3)

When the underlying `agent.stream(...)` raises an exception (LangGraph
recursion-limit abort, Gemini rate-limit, network timeout, etc.),
`astream` MUST yield exactly one
`AskPortfolioChunk(kind="error", error=str(exc))` as the terminal event
and MUST NOT yield a partial `AskPortfolioResult`. The SSE layer
(`interfaces/http/web/chat.py`) translates this to a final
`data: [ERROR]\n\n` frame so the client can render an inline
"connection lost, retry?" affordance. The client MUST NOT append the
partial assistant text to `localStorage`.

#### Scenario: Mid-stream exception yields a single ERROR chunk

- GIVEN the agent raises `RecursionLimitExceeded` after 2 token chunks
- WHEN `astream` is iterated
- THEN it MUST yield exactly 2 `kind="token"` chunks (one per delivered
  `AIMessageChunk`)
- AND MUST yield exactly 1 terminal
  `AskPortfolioChunk(kind="error", error="RecursionLimitExceeded(...)")`
- AND MUST NOT yield any `kind="done"` chunk.

#### Scenario: Mid-stream exception does not yield a partial result

- GIVEN the agent raises mid-stream
- WHEN the caller inspects the iterator
- THEN no yielded chunk MUST have a non-`None` `result` field
- AND `AskPortfolioResult` MUST NOT be constructed for the failed stream.

---

## LLM Prompt Discipline

This section is the cross-cutting prompt-engineering principle (Decision
#12) applied to every LLM-facing call in the playground. Three legs:

1. **Scoped** — pass only the minimum context the agent needs. No
   "you are an expert assistant" boilerplate unless it is load-bearing
   for correctness.
2. **Short-first** — default `max_tokens` is the minimum that completes
   the typical answer, not the maximum the model can generate.
3. **Complete on the critical path** — never sacrifice correctness or
   user understanding for terseness.

### Requirement: max_tokens Defaults Are Reduced (Short-First)

The default `max_tokens` for each tool MUST be reduced from the previous
value by ~30 % where the typical answer fits, and MUST NOT be reduced
below what correctness requires. The shipped defaults are:

| Tool | Previous default | New default |
|---|---|---|
| `explain_architecture` | 500 | **350** |
| `summarize_readme` | 300 | **200** |
| `ask_portfolio` | (no explicit default) | **600** (`UsageLimits(response_tokens_limit=600)`) |

#### Scenario: explain_architecture defaults to 350 tokens

- GIVEN a fresh request without an explicit `max_tokens` argument
- WHEN `ExplainArchitectureUseCase.execute` is invoked
- THEN the `LLMPort.summarize` call MUST be made with
  `max_tokens=350`
- AND a unit test MUST assert this default.

#### Scenario: summarize_readme defaults to 200 tokens

- GIVEN a fresh request without an explicit `max_tokens` argument
- WHEN `SummarizeReadmeUseCase.execute` is invoked
- THEN the `LLMPort.summarize` call MUST be made with
  `max_tokens=200`
- AND a unit test MUST assert this default.

#### Scenario: ask_portfolio caps at 600 tokens

- GIVEN the agent is invoked with no explicit usage cap
- WHEN `AskPortfolioUseCase.aexecute` (or `astream`) is invoked
- THEN the agent MUST be invoked with a 600-token response cap
- AND a unit test MUST assert this default.

#### Scenario: Explicit override is honored

- GIVEN the caller passes `max_tokens=1000` to `explain_architecture`
- WHEN the use case runs
- THEN the LLM call MUST use the caller-supplied value (350 is only the
  default, not a hard cap).

---

## Configuration & Build

### Requirement: Sanitizer Middleware Skip-List (7 Prefixes)

Per `security-layers` § Middleware skip-list, the
`OutputSanitizerMiddleware.SKIP_PATH_PREFIXES` tuple at
`src/mcp_server/interfaces/http/middleware/sanitizer.py` MUST equal:

```python
SKIP_PATH_PREFIXES: tuple[str, ...] = (
    "/healthz",
    "/mcp",
    "/chat",
    "/chat/stream",
    "/playground",
    "/playground/api",
    "/static",
)
```

- `/healthz`, `/mcp` — pre-existing (probe + MCP governed by tool-layer
  sanitizer).
- `/chat`, `/chat/stream` — chat HTML + SSE bytes (middleware buffers;
  would break per-token latency).
- `/playground`, `/playground/api` — playground HTML + per-tool
  fragments (the use case's own `sanitize(...)` is the only Layer 3 pass
  allowed; double-pass would corrupt already-sanitized payloads).
- `/static` — vendored HTMX + `style.css` (regex redaction would corrupt
  the JS bytes).

Adding a new prefix requires a spec change (closed-world contract). The
unit test asserts the exact tuple.

#### Scenario: Skip-list is a 7-tuple

- GIVEN `sanitizer.py` is imported
- WHEN `SKIP_PATH_PREFIXES` is inspected
- THEN it MUST equal the 7-tuple listed above exactly.

#### Scenario: Non-skipped routes still pass through sanitizer

- GIVEN any route that is NOT a prefix in `SKIP_PATH_PREFIXES`
- WHEN the route returns a response body containing an AWS-shaped key
- THEN the middleware MUST rewrite the body
- AND a `RedactionIncident` with `pattern=aws` MUST be emitted
  (the middleware contract is unchanged for the rest of the surface).

### Requirement: Container Image Carries Playground Assets

Per `container-image` § Playground COPY, the runtime stage of the
Dockerfile MUST contain the entire `playground/` directory at
`/app/playground/` with the non-root user `mcp` as owner. The image MUST
contain at minimum: `playground/templates/base.html`,
`playground/templates/index.html`,
`playground/templates/playground.html`, `playground/templates/chat.html`
(and partials), and `playground/static/htmx.min.js` plus
`playground/static/style.css`.

> The Dockerfile COPY position is asserted by **relative position**
> (REL-13 amendment), not absolute line numbers: the
> `COPY --chown=mcp:mcp playground/ ./playground/` line MUST appear
> AFTER the existing `COPY scripts ./scripts` step and BEFORE
> `RUN python -m venv /opt/venv`. The runtime stage MUST contain a
> corresponding `COPY --from=builder` propagation.

The runtime image MUST have a compressed size under 500 MB. The current
baseline (post-005) is 417 MB; the playground additions are < 1 MB
total (HTMX 1.9.10 = 47,755 bytes / ~48 KB uncompressed — REL-6 corrects
the earlier ~14 KB figure, which was the gzipped wire size; Jinja2
templates ~20 KB; CSS ~6 KB; total playground delta ~75 KB).

#### Scenario: Runtime image contains the templates and HTMX

- GIVEN `docker build -t mcp-server:test .` completes
- WHEN `docker run --rm mcp-server:test ls -la /app/playground/` runs
- THEN the directory MUST exist
- AND `playground/templates/` MUST contain `base.html`
- AND `playground/static/` MUST contain `htmx.min.js`
- AND `playground/static/` MUST contain `style.css`.

#### Scenario: Built image size is below the 500 MB budget

- GIVEN the image is built
- WHEN `docker image ls mcp-server:test --format '{{.Size}}'` runs
- THEN the reported size MUST be < 500 MB (≈ 524,288,000 bytes).

#### Scenario: Vendored HTMX is in the image (no CDN fallback)

- GIVEN the runtime image is running
- WHEN `docker exec <container> cat /app/playground/static/htmx.min.js`
  runs
- THEN the command MUST exit 0
- AND the file MUST be > 10 KB
- AND the image MUST NOT contain any reference to a CDN URL for HTMX.

### Requirement: Fly.io Autoscale-to-Zero Stays Configured

`fly.toml` requires NO changes for the new routes. The existing
`auto_stop_machines = "stop"` and `min_machines_running = 0` settings
already autoscale to zero; SSE concurrency under the
`hard_limit = 50` (HTTP service `concurrency` setting in `fly.toml`)
is sufficient for the demo (≤ 50 simultaneous recruiters per machine).

### Requirement: Dependency Pins

`pyproject.toml` MUST declare:

- `langgraph>=0.2,<2.0` — keeps the streaming event surface stable per
  ADR-003.
- `fastapi>=0.135.0` — pins the floor for native `EventSourceResponse`
  via `fastapi.sse` (REL-8 amendment; PR2b bumped the pin from the
  original `>=0.115.0` because the native SSE re-export is only
  available at 0.135.0+ in the pinned Pydantic / Starlette stack).

---

## Error / Edge Cases

- A non-existent route under `/playground/api/...` MUST return 404 (not
  500); the route table is statically enumerated, so unknown tools fall
  through.
- If the composition root failed to wire a use case (programmer error),
  the route MUST return 500 with a sanitized message (no stack trace in
  the response body).
- Long-running LLM calls on `/playground/api/explain_architecture` and
  `/playground/api/summarize_readme` MUST keep the 30/min/IP slowapi
  ceiling (`security-layers` spec, Layer 5).
- A browser without JavaScript MUST still see a usable `GET /playground`
  page (forms degrade to native POSTs targeting the same endpoints); the
  chat page MUST show "JavaScript required" notice if `fetch` or
  `ReadableStream` is not supported.
- `JSON.parse` failure on a corrupted `localStorage` history entry: the
  client MUST treat the history as empty and continue (no crash, no wipe
  of `localStorage` without explicit user action).
- Two browser tabs in the same profile: both share the same
  `localStorage` history; the LAST tab to send a message wins
  (acceptable for a portfolio demo — no locking or broadcast channel
  required).
- `stream_mode="messages"` event with no `.content` attribute (empty
  chunk): MUST yield an `AgentChunk(kind="token", data="")` rather than
  raising (so the client doesn't see a disconnect on benign chunks).
- `--workers 1` is mandatory (per `app-bootstrap`) so the in-process
  rate-limit state is consistent across `/chat` and `/mcp` calls.
- `_MockLangChainAgentAdapter.stream` MUST NOT make any outbound HTTP
  call (zero network in mock mode).

---

## Test Scenarios

| Scenario | Required because |
|---|---|
| `GET /`, `GET /playground`, `GET /chat` all return 200 with `text/html` | Page surface contract |
| Each `/playground/api/{tool_name}` returns a fragment in < 500 ms (mock LLM) | Latency budget |
| `/chat/stream` delivers ≥ 2 chunks within 5 s in mock mode | SSE smoke (Decision #5) |
| `GET /static/htmx.min.js` is reachable from inside the container | Docker build correctness |
| Hexagonal invariant test stays GREEN (no new imports from `interfaces/` into `application/`) | Architecture invariant |
| `_MockLangChainAgentAdapter.stream` yields 5 tokens + DONE | Mock streaming contract (Decision #5) |
| `LangChainAgentAdapter.stream` uses `stream_mode="messages"` and yields `AIMessageChunk`-only | LangGraph integration |
| `AskPortfolioUseCase.astream` sanitizes each token before yielding | **Layer 3** per-chunk invariant (Decision #6) |
| Rate-limit gate fires once and blocks 31st request | **Layer 5** rate limit |
| Tool-call audit events emitted exactly once per invocation | **Layer 5** audit trail |
| DONE chunk carries sanitized `AskPortfolioResult` with `tools_called` | Final-result contract |
| Mid-stream exception yields exactly 1 `kind="error"` chunk and no partial result | REL-3 contract |
| SSE layer translates `kind="error"` to terminal `data: [ERROR]\n\n` | REL-3 wiring |
| `langgraph` version pin enforced by `tests/unit/test_pyproject.py` | Pin drift guard |
| MCP `/mcp` `ask_portfolio` continues to use `aexecute` (regression) | Buffered path unchanged |
| Server restart drops all conversation state (memory test) | Stateless-server contract (Decision #11) |
| `localStorage` round-trip preserves history across reloads | Client-side persistence |
| `/chat/stream` request body contains the full messages array | Full-history contract |
| `data: [DONE]` triggers exactly one append per assistant turn | Append-on-DONE contract |
| Private-mode browser degrades to single-message mode | Graceful degradation |
| No `Set-Cookie` header in `/chat` or `/chat/stream` responses | Privacy invariant |
| `explain_architecture` default `max_tokens == 350` | Short-first invariant |
| `summarize_readme` default `max_tokens == 200` | Short-first invariant |
| `ask_portfolio` default response cap == 600 | Short-first invariant |
| Caller-supplied `max_tokens` overrides the default | Override escape hatch |
| `SKIP_PATH_PREFIXES` equals the 7-tuple listed in this spec | Closed-world prefix set |
| Playwright smoke (gated `@pytest.mark.e2e`) loads `/playground`, submits one form, asserts the result `<div>` swaps | End-to-end happy path |

---

> Consolidated from change `003-playground-ui` (PR #1, PR #2, PR #3 on
> GitHub, archived 2026-08-06). Delta specs preserved at
> `openspec/changes/archive/2026-08-06-003-playground-ui/specs/`.
