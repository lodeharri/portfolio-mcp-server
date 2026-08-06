# Proposal: 003-playground-ui — Browser Playground + Streaming Chat

## Why

`002-mcp-tools` ships six working tools, but recruiters only see them if
they have Claude Desktop, Cursor, or MCP Inspector installed. Most
don't. This change exposes the same six tools through a browser-first
playground so a recruiter can land on a URL, ask "what projects are
indexed?", and watch a streamed ReAct answer appear — no installs, no
configuration, no Claude account.

The MCP transport at `/mcp` stays the canonical interface; the
playground is **a parallel HTTP surface** that reuses the same use cases
from `composition.py`. No business logic moves. No new ports. No new
adapters. Just three user-facing pages, a streaming variant of the
agent, and Solarized Phosphor-styled templates.

## Scope

### In Scope

1. **Three user-facing HTTP pages** plus two supporting endpoints,
   wired in `create_app()` between the existing `/healthz` mount and the
   `/mcp` sub-app mount (`app.py:84-87`):
   - `GET /` — landing page (project intro + CTAs to `/playground` and `/chat`).
   - `GET /playground` — renders five form cards (one per non-agent MCP tool).
   - `GET /chat` — chat-tab UI; SSE client connects to `/chat/stream`.
   - `POST /playground/api/{tool_name}` — five endpoints (one per
     non-agent tool) that invoke the matching use case from
     `request.app.state.composition` and return an HTML fragment
     (HTMX swap target).
   - `POST /chat/stream` — FastAPI native `EventSourceResponse` that
     streams `AIMessageChunk` events from the LangGraph ReAct agent.
2. **Streaming agent extension**: add `async def stream(...)` to
   `AgentPort` (new protocol method), implement on
   `LangChainAgentAdapter` via `agent.astream(..., stream_mode="messages")`,
   implement on `_MockLangChainAgentAdapter` (5 fake tokens + `done`
   sentinel with `asyncio.sleep(0.05)`). Add `astream` method to
   `AskPortfolioUseCase` that yields per-token `AskPortfolioChunk`s and
   sanitizes each chunk before yielding (Layer 3 invariant preserved).
3. **Jinja2 templates** under `playground/templates/`:
   `base.html`, `index.html`, `playground.html`, `chat.html`, partials
   for each tool result. **Solarized Phosphor** palette via CSS custom
   properties in `playground/static/style.css`.
4. **Vendored HTMX 1.9.10** at `playground/static/htmx.min.js`
   (no CDN, no network on page load; 47,755 bytes / ~48 KB
   uncompressed — ~14 KB gzipped on the wire per REL-6 amendment).
5. **Skip-list extension**: add `/chat`, `/chat/stream`, `/playground`,
   `/playground/api` to `SKIP_PATH_PREFIXES` at `sanitizer.py:39`
   (CRITICAL — current middleware buffers all bodies, which would break
   SSE token delivery).
6. **Dockerfile**: add `COPY --chown=mcp:mcp playground/ ./playground/`
   after `scripts` COPY at `Dockerfile:76` so the image ships templates
   and vendored HTMX.
7. **RED-first tests**: per-token sanitization test, `_MockLangChainAgentAdapter.stream`
   test, `EventSourceResponse` integration test (mock LLM + 5 tokens
   delivered), `TestClient` HTML fragment round-trip per playground form,
   sanitizer skip-list test, Playwright smoke gated behind `@pytest.mark.e2e`.

### Out of Scope

- **Conversation persistence** — `conversation_id` echoed but no history
  rendered between page reloads. Defer to `003.1-chat-history` (per-IP LRU).
- **WebSocket transport** — SSE is enough; demo doesn't need bidirectional.
- **Auth / rate-limit UI feedback** — slowapi still returns 429 via HTTP,
  no friendly toast.
- **Multi-model selector** — Gemini 2.0 Flash only.
- **Conversation export / copy-to-clipboard** — defer.
- **Per-project conversation isolation** — single global `/chat` session.
- **Custom landing-page portfolio** — landing page is minimal, not a
  replacement for the separate portfolio site.
- **Streaming for the five non-agent tools** — they return a single
  fragment; only `ask_portfolio` streams.
- **Fly.io deploy / custom domain** — `005-deploy` was deferred; this
  change only changes the image, not the deploy.

## Capabilities

### New

- `web-playground` — three HTTP routes, HTMX + Jinja2 server-rendered
  pages, SSE streaming endpoint. Reuses existing use cases from
  `composition.py`.

### Modified

- `app-bootstrap` — Adds a new router slot between `/healthz` and `/mcp`.
  Mount point is at the same position as the existing routers; the
  spec stays correct because the spec describes the factory + lifecycle,
  not the per-route list.
- `security-layers` — `OutputSanitizerMiddleware.SKIP_PATH_PREFIXES`
  grows from 2 entries to 6. The middleware contract (sanitize every
  non-skipped body) is unchanged.

## Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **HTMX 1.9.10 vendored at `playground/static/htmx.min.js`** | Zero CDN, zero network at page load, 47,755 bytes / ~48 KB uncompressed (~14 KB gzipped; REL-6 amendment). Matches the project's "no external runtime deps" discipline. Self-contained demo even when the platform blocks outbound. |
| 2 | **Jinja2 + server-rendered fragments** (no React/Vue) | Hexagonal: the templates are inputs to the HTTP adapter, not application code. No build step, no JSX, no SSR complexity. Jinja2 is already in `pyproject.toml:30`. |
| 3 | **FastAPI native `EventSourceResponse`** (no `sse-starlette`) | FastAPI 0.115+ ships `sse_starlette.sse.EventSourceResponse` re-export. Verified in context7 docs. Zero extra dep. |
| 4 | **`stream_mode="messages"` on LangGraph** | Latest stable event surface; yields `AIMessageChunk` directly. Pin `langgraph>=0.2,<2.0` so the contract can't drift mid-release. |
| 5 | **Mock agent streams 5 fake tokens** | Mock mode (no `GEMINI_API_KEY`) must still demo streaming. Deterministic fake tokens + `asyncio.sleep(0.05)` simulate ^real latency. Recruiters see the UI work without burning free tier RPM. |
| 6 | **Per-token sanitization in `AskPortfolioUseCase.astream`** | Layer 3 invariant says every byte leaving the server is sanitized. The middleware cannot do this for SSE (it buffers). The use case must own per-chunk sanitization. |
| 7 | **Single `/playground` page with form fragments** (no SPA) | Each tool's form action is `/playground/api/{tool_name}`; the response is an HTML fragment HTMX swaps into a result `<div>`. No page reload, no JSON marshalling on the client. |
| 8 | **No auth, no rate-limit UI** | Per user choice: recruiters land and try things. slowapi still 429s at 30/min/IP — silent, not friendly, but it works. |
| 9 | **Skip-list extension is local to `sanitizer.py:39`** | Tuple literal stays module-level; no DB or config-backed routing of skip prefixes. Adding 4 entries is a single edit + test update. |
| 10 | **Vendored HTMX is committed to the repo** | Reproducible builds (no CDN fetch in CI); the `Dockerfile` layer for `playground/` is deterministic. |
| 11 | **Chat history: stateful client + stateless server** | No auth → no user identity. Avoids IP-based tracking (GDPR concerns, dynamic IPs, surveillance-y in a portfolio piece). Client stores full history in `localStorage`; every `/chat/stream` request sends the entire conversation; the agent invokes with the full message list; server never persists. Recruiter owns their data; cross-device doesn't sync (honest, expected). Cost: ~40 LOC JS, **0 Python LOC**. |
| 12 | **LLM prompts: tight scope, short + complete** | Per user direction: every system/user prompt must be scoped to the minimum context needed, default to short answers, but never sacrifice completeness on the critical path. Default `max_tokens` reduced where reasonable (recruiter demo doesn't need 500-token essays on `explain_architecture`). Reduces per-query cost (matters on Fly.io pay-as-you-go) and improves UX. Specific calls in the spec phase. |

## Approach

One vertical slice (PR1: `playground` route + 5 forms + sanitizer skip extension)
plus one follow-up PR2 (streaming chat + agent port extension). Both
are ≲ 400 LOC so they fit the single-PR review budget.

**PR1 — Playground forms.**
- New `src/mcp_server/interfaces/http/web/__init__.py` package.
- `interfaces/http/web/playground.py` exports `build_web_router()` with
  five `POST /playground/api/{tool_name}` endpoints that call the
  matching use case from `request.app.state.composition` and render a
  Jinja2 fragment.
- `app.py` adds `app.include_router(build_web_router())` between
  `build_healthz_router()` (line 84) and `app.mount("/mcp", mcp_app)`
  (line 87).
- `interfaces/http/middleware/sanitizer.py:39` extends
  `SKIP_PATH_PREFIXES` to `("/healthz", "/mcp", "/chat", "/chat/stream",
  "/playground", "/playground/api")`.
- `playground/templates/` and `playground/static/style.css` populated.
- Tests: 5 form round-trips (TestClient), sanitizer skip-list, fragment
  HTML snapshots.

**PR2 — Streaming chat.**
- `application/ports/agent.py` adds `async def stream(...) -> AsyncIterator[AgentChunk]`
  to the `AgentPort` protocol.
- `infrastructure/langchain.py` implements `stream` on
  `LangChainAgentAdapter` (uses `agent.astream(..., stream_mode="messages")`)
  and on `_MockLangChainAgentAdapter` (5 fake tokens + `done`).
- `application/use_cases/ask_portfolio.py` adds `astream(...)` that
  per-token sanitizes and yields `AskPortfolioChunk(answer_token, done)`.
- `interfaces/http/web/chat.py` adds `GET /chat` (renders `chat.html`
  with an EventSource client) and `POST /chat/stream` (FastAPI
  `EventSourceResponse` wrapping `ask_portfolio_use_case.astream`).
- Test: `httpx.AsyncClient.stream(...)` reads ≥ 2 chunks within 5 s
  against the mock agent.
- **Chat persistence** (Decision #11): `playground/templates/chat.html`
  owns the conversation history in `localStorage`. JS reads the full
  history on page load, sends the entire `messages` array with each
  POST to `/chat/stream`, and appends the assistant reply to storage
  on `done` event. Server is stateless: receives full history, invokes
  the agent, returns the new response, stores nothing. ~40 LOC JS,
  **0 LOC Python**.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `src/mcp_server/app.py` | Modified | One new `include_router` call at line 84/87 slot |
| `src/mcp_server/interfaces/http/web/` | New | `playground.py`, `chat.py`, `__init__.py`, `deps.py` |
| `src/mcp_server/application/ports/agent.py` | Modified | New `stream` + `AgentChunk` types |
| `src/mcp_server/application/use_cases/ask_portfolio.py` | Modified | New `astream` method |
| `src/mcp_server/infrastructure/langchain.py` | Modified | `stream` on 2 adapter classes |
| `src/mcp_server/interfaces/http/middleware/sanitizer.py` | Modified | `SKIP_PATH_PREFIXES` +6 entries |
| `playground/templates/*.html` | New | 6 templates + partials |
| `playground/static/{style.css,htmx.min.js,solarized-phosphor.css}` | New | Solarized Phosphor palette + vendored HTMX 1.9.10 |
| `Dockerfile` | Modified | New `COPY` for `playground/` at line 77 |
| `tests/unit/interfaces/http/web/` | New | Per-form unit tests |
| `tests/integration/test_playground_*.py` | New | TestClient SST/HTML round-trip + SSE smoke |
| `tests/e2e/playground/test_*.py` | New | Playwright smoke (gated) |

### Prompt Engineering Principle (Decision #12)

Every LLM-facing prompt in the playground — the system prompt for the
agent, the per-tool instructions baked into the use cases, the default
`max_tokens` on each LLM call — must be:

1. **Scoped**: pass only the minimum context the agent needs to answer
   the recruiter's question. No "you are an expert assistant with access
   to many tools" boilerplate unless it's load-bearing.
2. **Short-first**: default `max_tokens` is the minimum that completes
   the typical answer, not the maximum the model can generate. Reduce
   the existing `explain_architecture=500` and `summarize_readme=300`
   defaults by ~30% if the typical answer fits.
3. **Complete on the critical path**: never sacrifice the answer's
   correctness or the user's understanding of it for terseness. If a
   recruiter asks "explain finance-coach-latam architecture" and the
   answer needs 400 tokens to be correct, 400 tokens is the right
   answer. The principle is "don't waste", not "truncate".

This is enforced at the spec level (each tool spec declares its
`max_tokens` default + system prompt content) and at the test level
(LLM call assertions check that the system prompt sent matches the
expected template).

## Risks

| # | Risk | L | Mitigation |
|---|------|---|------------|
| 1 | `OutputSanitizerMiddleware` buffers full bodies (`sanitizer.py:86-91`); SSE will appear dead to the user. | **H** | Add 4 paths to `SKIP_PATH_PREFIXES` at `sanitizer.py:39`. Per-token sanitization happens inside `AskPortfolioUseCase.astream` (Layer 3 invariant preserved). Integration test asserts ≥ 2 tokens reach the client within 5 s. |
| 2 | `_MockLangChainAgentAdapter` returns a fixed string and does not stream — recruiters with no `GEMINI_API_KEY` see no tokens. | **H** | Add `async def stream(...)` to the mock that yields 5 fake tokens + `done` sentinel with `asyncio.sleep(0.05)` between tokens. Unit test asserts iterator yields ≥ 5 chunks. |
| 3 | LangGraph `astream` event schema is volatile across releases. | **M** | Pin `langgraph>=0.2,<2.0` in `pyproject.toml:35`. Use `stream_mode="messages"` (current stable surface). Regression test pins the chunk shape (`AIMessageChunk` with `.content`). |
| 4 | No conversation state: `conversation_id` is echoed only; reloads lose history. | **M** | MVP keeps it opaque. Chat UI shows "each message is independent" hint. Per-IP LRU deferred to `003.1-chat-history`. |
| 5 | `playground/` directory is NOT in the Docker build context — templates + vendored HTMX will be missing from the production image. | **M** | Add `COPY --chown=mcp:mcp playground/ ./playground/` after `COPY scripts` (REL-13: relative position, not absolute line number) in `Dockerfile`. Add `tests/integration/test_docker_size.py` assertion that `playground/static/htmx.min.js` is reachable from inside the container. |
| 6 | `SKIP_PATH_PREFIXES` is a module-level tuple — adding paths requires edit + test update. | **M** | Single edit at `sanitizer.py:39`. List under "Affected Areas" so the spec test can pin the prefix set. |
| 7 | `TestClient` streaming tests need `httpx.AsyncClient.stream(...)` with deadlines. | **L** | Pin a 5 s deadline in the test; assert `response.status_code == 200` and ≥ 2 chunks received. Document the pattern in `tests/integration/conftest.py`. |
| 8 | Playwright E2E for SSE — Playwright is in optional `e2e` extras. | **L** | Gate with `@pytest.mark.e2e`; CI runs it on a separate job. Local dev uses `pip install -e ".[e2e]"`. |
| 9 | `app.mount("/mcp", ...)` ordering — possible collision with `/chat` or `/playground`. | **L** | No prefix overlap: `/` is the landing page, `/playground` and `/playground/api/*` are distinct, `/chat` and `/chat/stream` are distinct. FastAPI router resolution is order-independent. |
| 10 | CSP headers / cache-control for static assets — vendored HTMX, style.css. | **L** | Set `Cache-Control: public, max-age=3600` on `playground/static/*` via Starlette `StaticFiles`. CSP is permissive (`script-src 'self'`) since no inline JS. |
| 11 | Solarized Phosphor palette tokens need a confirmed reference (hex codes or style guide URL). | **L** | Vendor a `solarized-phosphor.css` with the canonical hex values from ethanschoonover.com/solarized. Final palette tokens captured in `style.css` under `:root { --solar-base03: #002b36; ... }`. |
| 12 | No conversation persistence between page reloads. | **L** | Documented in the UI ("each message is independent"). Defer to `003.1-chat-history`. No data loss — every request creates a fresh session. |

## Rollback

Additive. Revert PR1 → playground forms vanish, `/healthz` and `/mcp`
still work, agent stream gone. Revert PR2 → chat tab returns 404, ask
portfolio still works via MCP. No data loss. If the Dockerfile's
`playground/` COPY breaks the build, comment it out and the image
rebuilds (the routes will return 404 until fronted by web).

## Dependencies

- `001-bootstrap` ✅ shipped (FastAPI factory, sanitizer middleware, port binding).
- `002-mcp-tools` ✅ shipped (5 use cases + `ask_portfolio`).
- `005-langchain-integration` ✅ shipped (LangGraph ReAct agent, mock fallback).
- `jinja2>=3.1.4` ✅ in `pyproject.toml:30`.
- `python-multipart>=0.0.12` ✅ in `pyproject.toml:41`.
- `fastapi>=0.115.0` ✅ ships native `EventSourceResponse` (verified in context7).
- `langgraph>=0.2,<2.0` (pin to add at `pyproject.toml:35`).
- HTMX 1.9.10 vendored from `htmx.org` (manually downloaded once).

## Success Criteria

- [ ] `GET /` returns 200 with the landing page HTML in the production image.
- [ ] `GET /playground` returns 200 with 5 form cards rendered (one per
  non-agent MCP tool).
- [ ] Each `POST /playground/api/{tool_name}` returns a 200 HTML fragment
  in < 500 ms when the mock LLM is active.
- [ ] `GET /chat` returns 200 with the SSE client wired.
- [ ] `POST /chat/stream` delivers at least 2 `AIMessageChunk` tokens within
  5 s in mock mode (assertion in `tests/integration/test_chat_stream.py`).
- [ ] Playwright smoke (`@pytest.mark.e2e`) loads `/playground`, submits
  one form, asserts the result `<div>` swaps with the expected fragment.
- [ ] `OutputSanitizer` skip-list test GREEN — every playground and chat
  path is in `SKIP_PATH_PREFIXES`.
- [ ] Per-token sanitization test GREEN — `ask_portfolio.astream` runs
  each token through `OutputSanitizer.sanitize` before yielding.
- [ ] `docker build` succeeds; image size stays under 500 MB (current
  baseline: 417 MB post-005).
- [ ] Fly.io autoscale-to-zero keeps monthly spend at **< $1/month at
  10 visits/month** with the same machine class as `001-bootstrap`.
- [ ] Hexagonal invariant test stays GREEN (no new imports from
  `interfaces/` into `application/` or `domain/`).
- [ ] `pytest -q` passes; coverage stays ≥ 60 %.

## Cost Analysis

Fly.io machines autoscale-to-zero when idle. Per the Fly.io docs
(verified via context7), shared-cpu-1x 256 MB runs at
**$0.00000090/second** of active time. With `auto_stop_machines = "stop"`
and `min_machines_running = 0` already set in `fly.toml`, the machine
only charges while serving requests.

At 10 recruiter visits/month, each averaging ~2 minutes (browse landing,
fill one form, ask one chat question), the runtime is ~20 minutes/month:

```
$0.00000090/sec × 60 sec/min × 20 min = $0.00108/month
```

Plus a small monthly base allocation — well under **$1/month total** at
this traffic level. The three routes add no new infra: SSE uses the
same HTTP connection, HTMX is vendored, Gemini stays on free tier (mock
fallback when no `GEMINI_API_KEY`).

No paid services introduced. No new deps that grow the image
significantly (HTMX 1.9.10 = 47,755 bytes / ~48 KB uncompressed; total
playground delta ~75 KB including CSS + templates + partials; REL-6
amendment).

## Open Questions

The spec / design phases should confirm these with the user before
implementation:

1. **Solarized Phosphor hex tokens** — confirm the canonical palette
   (ethanschoonover.com base03 / base02 / base01 / base00 / base0 / base1
   / base2 / base3, plus the 8 accent colors) is the right reference, or
   pull from the landing-page-portfolio site if it already defines
   branded tokens.
2. **Landing page content** — should `/` be a near-empty "click here" or
   a richer "what this is, why it matters, two CTAs" landing? Owner
   supplies the prose.
3. **`ask_portfolio` streaming — replace or add?** Add `astream` (the
   current plan), or replace `run` entirely with `stream` since
   `ask_portfolio` is never called via MCP in the playground? The MCP
   tool still uses `run` (final token only). Confirm: keep both.
4. **Concurrent chat sessions** — resolved by Decision #11: stateful
   client + stateless server means each browser tab has its own
   conversation in `localStorage`. `window.crypto.randomUUID()` is the
   session key. Server has no idea sessions exist.
5. **HTMX version pin** — 1.9.10 (latest stable at SDD time) or pin a
   minimum `>=1.9.0,<2.0`?
6. **SSE timeout** — cap the stream at 60 s? `EventSourceResponse` on
   FastAPI inherits uvicorn's keep-alive; if a Gemini call stalls, the
   client times out at 5 min by default. Confirm acceptable.
7. **CSP / headers** — set `Content-Security-Policy: default-src 'self';
   script-src 'self'; style-src 'self'`, or skip CSP for MVP and add it
   in `003.1`?

## References

- FastAPI SSE tutorial — `https://fastapi.tiangolo.com/advanced/custom-response/#eventstoreresponse` — confirms `EventSourceResponse` is native to FastAPI 0.115+ (no `sse-starlette` dep).
- LangGraph streaming — `https://langchain-ai.github.io/langgraph/concepts/streaming/` — `stream_mode="messages"` yields `AIMessageChunk` events; stable contract.
- LangGraph `astream` reference — `https://langchain-ai.github.io/langgraph/reference/graphs/#langgraph.graph.state.CompiledStateGraph.astream` — `astream(input, config, stream_mode="messages", subgraphs=True)`.
- Fly.io autoscale-to-zero — `https://fly.io/docs/apps/autoscaling/` — `auto_stop_machines = "stop"` + `min_machines_running = 0` confirmed in `fly.toml`.
- Fly.io pricing — `https://fly.io/docs/about/pricing/` — shared-cpu-1x 256 MB at $0.00000090/sec.
- HTMX 1.9.10 — `https://htmx.org/` — vendored MIT-licensed file, no CDN.
- Solarized Phosphor palette — `https://ethanschoonover.com/solarized/` — base / accent hex values.
- Existing spec `app-bootstrap` — `openspec/specs/app-bootstrap/spec.md` — router insertion ordering.
- Existing spec `mcp-tools` — `openspec/specs/mcp-tools/spec.md` — use case contracts.
- Existing spec `security-layers` — `openspec/specs/security-layers/spec.md` — Layer 3 sanitizer middleware contract.
