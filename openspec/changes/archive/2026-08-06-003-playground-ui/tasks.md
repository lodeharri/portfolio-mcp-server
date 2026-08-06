# Tasks: 003-playground-ui

## Delivery Strategy

**3 PRs**, each independently mergeable, sequenced: PR1 → PR2a → PR2b.

### Review Workload Forecast

| PR | Scope | Prod LOC | Test LOC | Total | Budget | Status |
|----|-------|----------|----------|-------|--------|--------|
| PR1 | Playground forms + skip-list + Docker | ~340–380 | ~180–210 | ~520–590 | 400 (prod) | ✅ under budget |
| PR2a | Streaming chat backend | ~250–280 | ~140–170 | ~390–450 | 400 (prod) | ✅ under budget |
| PR2b | Streaming chat frontend | ~110–130 | ~60–80 | ~170–210 | 400 (prod) | ✅ under budget |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

Each PR carries its own `config.yaml` / `pyproject.toml` / `Dockerfile` edits when they apply. PR1 lands first, PR2a can be tested independently through the mock adapter, and PR2b wires the browser to the already-tested SSE backend.

---

## PR1 — Playground Forms

### 1.0 Configuration

- [x] 1.0.1 **Amend `openspec/config.yaml:33`**: relax invariant #7 from `<150 MB` to `<500 MB`; comment that Python + AI dependencies, LangGraph, and sqlite-vec require the current headroom, with Alpine migration deferred.
- [x] 1.0.2 **Amend `openspec/config.yaml:58`**: remove `Streaming chat over HTMX uses htmx-ws`; comment that this change uses native browser SSE/fetch with FastAPI `EventSourceResponse` per Decision #12 and ADR-002.

### 1.1 Middleware skip-list extension

- [x] 1.1.1 **RED** — extend `tests/unit/interfaces/http/test_middleware.py` to assert the closed-world skip tuple includes `/healthz`, `/mcp`, `/chat`, `/chat/stream`, `/playground`, `/playground/api`, and `/static`; assert `/healthcheck` is not skipped; confirm the new cases fail.
- [x] 1.1.2 **GREEN** — extend `SKIP_PATH_PREFIXES` at `src/mcp_server/interfaces/http/middleware/sanitizer.py:39` with the four chat/playground prefixes and `/static`; run the focused middleware tests.
- [x] 1.1.3 **REFACTOR** — keep the prefix list as one module-level tuple and update its explanatory docstring; verify `python3 -m ruff check src/mcp_server/interfaces/http/middleware tests/unit/interfaces/http/test_middleware.py`.

> The approved `/static/*` decision makes the closed-world set **7 entries**, not 6: it is the fifth new prefix alongside the four chat/playground prefixes.

### 1.2 Vendored HTMX + static assets

- [x] 1.2.1 Downloaded the exact HTMX 1.9.10 minified artifact to `playground/static/htmx.min.js` (47,755 bytes — the spec's ~14 KB figure is the gzipped wire size per Decision #1). Embedded `version:"1.9.10"` marker verified; legacy `/* htmx.org */` banner is absent in htmx 1.x — banner assertion replaced with version-string match.
- [x] 1.2.2 **RED** — add `tests/unit/interfaces/http/web/test_static.py` asserting `/static/htmx.min.js` returns 200, exact vendored bytes, the HTMX banner, no CDN reference, and `Cache-Control: public, max-age=31536000, immutable`.
- [x] 1.2.3 **GREEN** — mount `playground/static/` at `/static/` from `src/mcp_server/interfaces/http/web/router.py`; apply the immutable cache header without altering asset bytes.

### 1.3 Solarized Phosphor style

- [x] 1.3.1 Create `playground/static/style.css` with the eight canonical Solarized base tokens and eight accent tokens; comment every custom property with its canonical hex value.
- [x] 1.3.2 **RED** — extend `tests/unit/interfaces/http/web/test_static.py` to assert `/static/style.css` returns 200, references all 16 `:root` tokens, uses the canonical hex values, and has the immutable cache header.
- [x] 1.3.3 **GREEN/REFACTOR** — serve `style.css` through the shared static mount; ensure no external stylesheet/CDN dependency and run `ruff` plus asset formatting checks.

### 1.4 Templates (base + landing)

- [x] 1.4.1 Create `playground/templates/base.html` with the Solarized Phosphor navigation, `/static/style.css`, `/static/htmx.min.js`, and `{% block content %}`.
- [x] 1.4.2 Create `playground/templates/index.html` extending `base.html`; render `list_projects` output and CTAs to `/playground` and `/chat`, including the zero-index fallback display.
- [x] 1.4.3 **RED** — add `tests/integration/test_web_routes.py` cases for `GET /`: 200 `text/html`, one `/playground` link per project, both CTAs, and `index_chunk_count == 0` when the SQLite index is absent.
- [x] 1.4.4 **GREEN** — implement `GET /` in `src/mcp_server/interfaces/http/web/router.py` using the shared Jinja2 environment and `request.app.state.composition.list_projects_use_case`.

### 1.5 Templates (playground)

- [x] 1.5.1 Create `playground/templates/playground.html` extending `base.html` with exactly five form cards and per-card `hx-post`, `hx-target`, and `hx-swap` attributes for the five non-agent tools.
- [x] 1.5.2 **RED** — extend `tests/integration/test_web_routes.py` to assert `GET /playground` returns 200 `text/html`, exactly five forms, all five endpoint targets, both local static assets, and no HTMX CDN URL.
- [x] 1.5.3 **GREEN** — implement `GET /playground` in `router.py`; confirm native form submission remains usable when JavaScript is unavailable.

### 1.6 Web router skeleton

- [x] 1.6.1 Create empty package marker `src/mcp_server/interfaces/http/web/__init__.py`.
- [x] 1.6.2 Create `src/mcp_server/interfaces/http/web/router.py` with `build_web_router() -> APIRouter`, shared `Jinja2Templates`, static mount, and composition lookup through `request.app.state.composition`.
- [x] 1.6.3 Wire `app.include_router(build_web_router())` in `src/mcp_server/app.py:85`, between the healthz router and `/mcp` mount.
- [x] 1.6.4 **RED** — extend `tests/integration/test_app_factory.py` to assert `create_app(config).url_path_for("landing")` resolves `/` and the web router is present without changing the MCP mount.
- [x] 1.6.5 **GREEN/REFACTOR** — run the web-route and hexagonal-invariant tests; keep all concrete adapter wiring in `composition.py` and web imports limited to application-facing objects.

### 1.7 Form endpoints (one per non-agent tool)

- [x] 1.7.1 **RED** — in `tests/unit/interfaces/http/web/test_playground_forms.py`, assert `POST /playground/api/list_projects` invokes the use case and returns a non-empty Jinja2 fragment containing project IDs, with no `<html>`/`<body>` wrapper.
- [x] 1.7.2 **GREEN** — add the `list_projects` route in `src/mcp_server/interfaces/http/web/playground.py` and render `playground/templates/partials/list_projects.html`.
- [x] 1.7.3 **RED** — test `POST /playground/api/search_code` with `query=rate limit` renders three sanitized matches with `file_path` and `content`.
- [x] 1.7.4 **GREEN** — implement the `search_code` route and `playground/templates/partials/search_code.html`.
- [x] 1.7.5 **RED** — test `POST /playground/api/explain_architecture` with `project_id` renders the display name, summary, and source anchors; unknown projects return a sanitized 4xx fragment.
- [x] 1.7.6 **GREEN** — implement the `explain_architecture` route and `playground/templates/partials/explain_architecture.html`.
- [x] 1.7.7 **RED** — test `POST /playground/api/summarize_readme` renders display name, one-paragraph summary, and README source link for a declared project.
- [x] 1.7.8 **GREEN** — implement the `summarize_readme` route and `playground/templates/partials/summarize_readme.html`.
- [x] 1.7.9 **RED** — test `POST /playground/api/get_architecture_diagram` renders sanitized inline SVG plus a “view full diagram” link for a declared diagram.
- [x] 1.7.10 **GREEN** — implement the diagram route and `playground/templates/partials/architecture_diagram.html`; map use-case failures to HTML fragments, not raw JSON or tracebacks.
- [x] 1.7.11 **REFACTOR** — centralize form parsing, use-case lookup, and error-to-fragment handling in the web adapter; assert unknown `/playground/api/...` routes remain 404 and no new infra imports enter `interfaces/http/web/`.

### 1.8 LLM prompt discipline (PR1 subset)

- [x] 1.8.1 **RED** — extend `tests/unit/application/use_cases/test_explain_architecture.py` to assert the default LLM call uses `max_tokens=350`, the scoped prompt has the expected output shape, and an explicit override remains honored.
- [x] 1.8.2 **GREEN** — change `ExplainArchitectureRequest.max_tokens` at `src/mcp_server/application/use_cases/explain_architecture.py:19` from 500 to 350 without truncating required ADR context.
- [x] 1.8.3 **RED** — extend `tests/unit/application/use_cases/test_summarize_readme.py` to assert the default call uses `max_tokens=200`, the prompt is scoped to a recruiter paragraph, and an explicit override remains honored.
- [x] 1.8.4 **GREEN** — change `SummarizeReadmeRequest.max_tokens` at `src/mcp_server/application/use_cases/summarize_readme.py:19` from 300 to 200.
- [x] 1.8.5 **RED** — ~~add prompt fixture assertions~~ Deferred to follow-up: the orchestrator's PR1 prompt scoped LLM prompt discipline to the `max_tokens` reductions (500→350 and 300→200); system-prompt fixture files are out of PR1 scope.
- [x] 1.8.6 **GREEN/REFACTOR** — ~~≤200-word scoped fixtures~~ Deferred alongside 1.8.5 (see note above). `max_tokens` reductions shipped; fixture pinning is a later change.

### 1.9 Dockerfile

- [x] 1.9.1 Add `COPY --chown=mcp:mcp playground/ ./playground/` after `COPY scripts ./scripts` at `Dockerfile:76`, and propagate `/build/playground` into the runtime stage after `Dockerfile:125` so `/app/playground` exists.
- [x] 1.9.2 **RED** — add gated `tests/integration/test_docker_playground.py` that builds `mcp-server:test` when Docker is available and asserts non-empty `/app/playground/templates/index.html`, `base.html`, `style.css`, HTMX banner, ownership, and image size under 500 MB.
- [x] 1.9.3 **GREEN/REFACTOR** — confirm `docker build -t mcp-server:test .` and a `docker run --rm -p 8080:8080 mcp-server:test` smoke request; do not modify `fly.toml`.

### 1.10 PR1 commit + push

- [x] 1.10.1 Prepare conventional commit `feat(web): browser playground with 5 MCP tool forms`; do not add AI attribution or `Co-Authored-By:`.
- [x] 1.10.2 Open PR1 against `main` only after the acceptance gates; explain skip-list/static rationale, vendored HTMX, image-budget amendment, and prompt-discipline changes.

---

## PR2a — Streaming Chat Backend

### 2a.0 Version pin

- [ ] 2a.0.1 **RED → GREEN** — add `tests/unit/test_pyproject.py` asserting `langgraph` is constrained to `>=0.2,<2.0`; tighten `pyproject.toml:35` and verify `pip install -e ".[dev]"` satisfies the range.

### 2a.1 Agent port extension

- [ ] 2a.1.1 **RED** — add `tests/unit/application/ports/test_agent_port.py` asserting Pydantic `AgentChunk` accepts only `token`/`tool_call`/`done`, and `AgentPort.stream` has the async iterator signature without changing `run`.
- [ ] 2a.1.2 **GREEN/REFACTOR** — add `AgentChunk` and additive `stream(self, request: AgentRequest, tools: list[Any]) -> AsyncIterator[AgentChunk]` to `src/mcp_server/application/ports/agent.py`; preserve the existing buffered contract.

### 2a.2 Real adapter stream

- [ ] 2a.2.1 **RED** — add `tests/unit/infrastructure/test_langchain_adapter_stream.py` with a stub LangGraph yielding AI and non-AI messages; assert only `AIMessageChunk` events become tokens, `stream_mode="messages"`, and recursion limit is `max_tool_calls * 2 + 1`.
- [ ] 2a.2.2 **GREEN** — implement `LangChainAgentAdapter.stream` in `src/mcp_server/infrastructure/langchain.py` using `agent.astream(input, config, stream_mode="messages")`, yielding token chunks and a terminal done chunk.
- [ ] 2a.2.3 **REFACTOR** — extract message filtering only if it exceeds five lines; retain mock/real `AgentChunk` parity and no outbound calls in mock mode.

### 2a.3 Mock adapter stream

- [ ] 2a.3.1 **RED** — extend the adapter stream test to require exactly five tokens `("Tok", "en", "ized", " mock", " answer")`, each spaced by at least 0.05s, then one `kind="done"` chunk.
- [ ] 2a.3.2 **GREEN** — implement `_MockLangChainAgentAdapter.stream` with the deterministic sequence and `asyncio.sleep(0.05)` between tokens.

### 2a.4 Use case extension

- [ ] 2a.4.1 **RED** — add `tests/unit/application/use_cases/test_astream_ask_portfolio.py` covering one rate-limit check before agent iteration, blocked requests without agent calls, per-token sanitizer calls/redaction, tool-call audit events, and no partial result on mid-stream failure.
- [ ] 2a.4.2 **GREEN/REFACTOR** — add `AskPortfolioChunk` and `AskPortfolioUseCase.astream` in `src/mcp_server/application/use_cases/ask_portfolio.py`; reuse the rate gate, sanitize each token, audit tool calls, accumulate sanitized text, and yield the final result with `tools_called` and `conversation_id`.

### 2a.5 LLM prompt discipline (PR2a subset)

- [ ] 2a.5.1 **RED** — extend `tests/unit/application/use_cases/test_ask_portfolio.py` and add `tests/unit/composition/test_agent_prompt.py` to assert `UsageLimits(response_tokens_limit=600)` and the scoped five-tool agent prompt fixture.
- [ ] 2a.5.2 **GREEN** — set `response_tokens_limit=600` in the Pydantic AI agent configuration and add `tests/fixtures/prompts/ask_portfolio_agent_system.txt` without generic helpful-assistant boilerplate.

### 2a.6 Integration test for the streaming path

- [ ] 2a.6.1 **RED** — add `tests/integration/test_chat_streaming.py` calling `AskPortfolioUseCase.astream` through the real composition in mock mode; require at least two chunks within five seconds and no raw secret token.
- [ ] 2a.6.2 **GREEN/REFACTOR** — confirm `create_app(AppConfig(gemini_api_key=""))` wires the mock stream, the buffered `run`/`aexecute` path remains green, and hexagonal invariants still report 6/6.

### 2a.7 PR2a commit + push

- [ ] 2a.7.1 Prepare conventional commit `feat(agent): streaming variant of ask_portfolio use case`; do not add AI attribution.
- [ ] 2a.7.2 Open PR2a after acceptance gates; explain additive port design, adapter mock parity, version pin, and per-token sanitization rationale.

---

## PR2b — Streaming Chat Frontend

### 2b.1 Chat template

- [ ] 2b.1.1 Create `playground/templates/chat.html` extending `base.html` with input form, message list, connection-status indicator, JavaScript-required notice, and localStorage persistence notice.
- [ ] 2b.1.2 Embed the chat client script inline in `chat.html`; use the native browser SSE contract over `fetch`/`ReadableStream` because POSTing the full messages array is required (not HTMX or a CDN client).

### 2b.2 Chat client JS (localStorage + SSE)

- [ ] 2b.2.1 Implement session bootstrap: read `mcp-playground-chat:sid`, generate with `window.crypto.randomUUID()` when absent, and namespace history as `mcp-playground-chat:<uuid>:history`.
- [ ] 2b.2.2 Implement guarded history load/render before input acceptance; corrupted JSON becomes empty history without crashing or silently deleting storage.
- [ ] 2b.2.3 Implement submit/request construction: append the new user message, POST the complete `messages` array plus `conversation_id` to `/chat/stream`, and set `Accept: text/event-stream`.
- [ ] 2b.2.4 Implement incremental SSE parsing/rendering and accumulation; on `data: [DONE]`, append exactly one assistant message and persist the complete history.
- [ ] 2b.2.5 Implement connection-drop handling: if EOF or `[ERROR]` arrives before `[DONE]`, do not persist partial assistant text and show inline “connection lost, retry?” affordance.
- [ ] 2b.2.6 Implement graceful degradation around every localStorage read/write: show “Conversations in this browser are not saved between reloads.” and send only the current user message in single-message mode.

### 2b.3 Chat route tests

- [ ] 2b.3.1 **RED** — add gated `tests/e2e/playground/test_chat.py` covering page load, streamed answer, localStorage-backed reload history, full-history request body, DONE-only append, connection-drop retry affordance, and private-mode single-message degradation.

### 2b.4 Chat routes

- [ ] 2b.4.1 **RED** — add `tests/unit/interfaces/http/web/test_chat_routes.py` asserting `GET /chat` returns 200 `text/html`, contains the chat input, inline script, and `/chat/stream` target.
- [ ] 2b.4.2 **GREEN** — implement `GET /chat` in `src/mcp_server/interfaces/http/web/chat.py` rendering `chat.html` through the shared template environment.
- [ ] 2b.4.3 **RED** — extend the route test with a valid `POST /chat/stream` assertion for `text/event-stream`, at least two `data:` events, terminal `data: [DONE]`, `Cache-Control: no-cache`, `X-Accel-Buffering: no`, and no `Set-Cookie` header.
- [ ] 2b.4.4 **GREEN** — implement `POST /chat/stream` with FastAPI's native `EventSourceResponse`, mapping token chunks to SSE data frames and the done chunk to `[DONE]`; do not persist session state or log message content/UUIDs.

### 2b.5 Wire chat router

- [ ] 2b.5.1 Add chat routes to `build_web_router()` in `src/mcp_server/interfaces/http/web/router.py` without changing composition ownership or the `/mcp` mount.
- [ ] 2b.5.2 **RED** — extend `tests/integration/test_web_routes.py` to assert `GET /chat` is 200 HTML and an async streamed `POST /chat/stream` delivers ≥2 chunks within five seconds in mock mode.
- [ ] 2b.5.3 **GREEN/REFACTOR** — run the full suite, integration hexagonal invariants, and a no-cookie/stateless-server regression using two fresh `create_app()` instances.

### 2b.6 PR2b commit + push

- [ ] 2b.6.1 Prepare conventional commit `feat(chat): streaming chat UI with localStorage persistence`; do not add AI attribution.
- [ ] 2b.6.2 Open PR2b after acceptance gates; explain stateful-client/stateless-server (ADR-004), native SSE over HTMX (ADR-002), localStorage keys, and graceful degradation.

---

## Acceptance Gates (run before each PR's `gh pr create`)

For every PR:

- [ ] `pytest -q` → existing 479 + new tests pass with no regressions.
- [ ] `python3 -m ruff check src/mcp_server tests/` → all checks passed.
- [ ] `python3 -m pytest tests/integration/test_hexagonal_invariants.py -v` → 6/6 GREEN.
- [ ] Confirm `interfaces/http/web/` imports application ports/use cases only; no infrastructure imports enter the interface layer.
- [ ] Manual smoke: `python3 -c "from mcp_server.app import create_app; from mcp_server.config import AppConfig; create_app(AppConfig(gemini_api_key=''))"` confirms composition wiring.
- [ ] PR1: `docker build -t mcp-server:test .` succeeds; image is <500 MB; `/static/htmx.min.js` is reachable from the image.
- [ ] PR2a: mock-composition integration test confirms `astream`; MCP buffered `ask_portfolio` remains green.
- [ ] PR2b: `httpx.AsyncClient.stream("POST", "/chat/stream")` reads ≥2 chunks within five seconds and ends with `[DONE]`.

## Out of Scope (deferred)

- Conversation persistence across devices — localStorage only; server remains stateless.
- CSP headers — deferred to 003.1.
- SSE timeout cap — inherits uvicorn defaults; revisit if abuse appears.
- Alpine migration to reduce image below 150 MB — deferred to a future change.
- Per-IP rate-limit UI affordance — silent 429, not friendly.
- Fly.io or custom-domain deployment changes — `fly.toml` remains unchanged.

## Open Questions Surfaced During Decomposition

- **FastAPI compatibility floor**: the current environment exposes `EventSourceResponse` from `fastapi.sse`, but Context7's current FastAPI documentation says native SSE support starts at 0.135.0 while `pyproject.toml:27` only requires `fastapi>=0.115.0`. PR2b must verify the installed floor or explicitly tighten the dependency before relying on the native response.
- **`UsageLimits` architecture mismatch**: the current `AskPortfolioUseCase` and `infrastructure/langchain.py` use LangGraph/LangChain, not Pydantic AI, so `UsageLimits(response_tokens_limit=600)` is not currently an available call boundary. The approved field name is recorded in 2a.5, but apply must reconcile it with the shipped LangChain architecture without violating the additive streaming decision.
