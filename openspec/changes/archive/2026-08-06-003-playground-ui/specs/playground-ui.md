# playground-ui — Delta Specification

## Purpose

The browser playground that exposes the existing six MCP tools to recruiters
through three HTTP pages plus two supporting endpoints. A recruiter lands on
`/`, clicks into `/playground` or `/chat`, and exercises the same use cases
that the MCP transport at `/mcp` already serves. The MCP transport remains
the canonical interface; the playground is a parallel HTTP surface that
**reuses use cases from `composition.py`** without moving business logic and
without adding new ports or adapters.

Per Decisions #1, #2, #3, #7: HTMX 1.9.10 is vendored at
`playground/static/htmx.min.js` (~48 KB / 47,755 bytes uncompressed;
~14 KB gzipped on the wire — REL-6 amendment; no CDN), templates are
server-rendered Jinja2 fragments, FastAPI's native `EventSourceResponse`
handles SSE, and each non-agent tool has its own
`POST /playground/api/{tool_name}` form endpoint that returns an HTML
fragment HTMX swaps into a result `<div>`. The total playground delta
(HTMX + CSS + templates + partials) is ~75 KB uncompressed, well under
the 1 MB image-budget allowance.

The new surface lives in a fresh `src/mcp_server/interfaces/http/web/`
package: `playground.py` (forms), `chat.py` (streaming), `deps.py` (shared
use case lookup), `__init__.py`. The router is mounted in `app.py:84-87`
between `build_healthz_router()` and `app.mount("/mcp", mcp_app)`.

## Schema / Interface

```python
# src/mcp_server/interfaces/http/web/playground.py
from fastapi import APIRouter, Request, Form

def build_web_router() -> APIRouter:
    """Build the playground router (forms + landing + chat)."""
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def landing(request: Request) -> HTMLResponse: ...

    @router.get("/playground", response_class=HTMLResponse)
    async def playground_index(request: Request) -> HTMLResponse: ...

    @router.post("/playground/api/list_projects",
                 response_class=HTMLResponse)
    async def list_projects_fragment(request: Request) -> HTMLResponse: ...

    # …one POST per non-agent tool (5 total)…

    @router.get("/chat", response_class=HTMLResponse)
    async def chat_page(request: Request) -> HTMLResponse: ...

    @router.post("/chat/stream")
    async def chat_stream(request: Request) -> EventSourceResponse: ...

    return router

# src/mcp_server/app.py (insertion between lines 84 and 87)
app.include_router(build_web_router())
```

```html
<!-- playground/templates/base.html -->
<script src="/static/htmx.min.js" defer></script>
<link rel="stylesheet" href="/static/style.css">
```

## ADDED Requirements

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

`GET /playground` MUST render five form cards — one per non-agent MCP tool
(`list_projects`, `search_code`, `explain_architecture`, `summarize_readme`,
`get_architecture_diagram`). Each card MUST submit to its dedicated
`POST /playground/api/{tool_name}` endpoint with `hx-post`/`hx-target`/
`hx-swap` attributes so HTMX swaps the response into a per-card result
`<div>` without a full page reload.

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
the middleware skip-list (see `sanitizer-skip-list` spec) ensures no
double-sanitization.

The five endpoints MUST be:
`list_projects`, `search_code`, `explain_architecture`, `summarize_readme`,
`get_architecture_diagram`.

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
state in `localStorage` per Decision #11: stateful client + stateless
server. The script MUST issue a `fetch` against `/chat/stream` with
`Accept: text/event-stream` and append each `data:` chunk to the visible
transcript.

#### Scenario: Chat page returns 200 with SSE client wired

- GIVEN the server is running
- WHEN a browser sends `GET /chat`
- THEN the response MUST be 200 with `Content-Type: text/html`
- AND the HTML MUST include a `<script>` block that POSTs to
  `/chat/stream`
- AND MUST reference `/static/htmx.min.js` (HTMX can be used alongside
  the custom EventSource client for the non-chat cards, but `/chat`
  uses native EventSource).

### Requirement: Streaming Endpoint at POST /chat/stream

`POST /chat/stream` MUST accept a JSON body of the form
`{"messages": [{"role": "user", "content": "..."}, ...]}`,
invoke `AskPortfolioUseCase.astream` (see `agent-streaming` spec), and
return an `EventSourceResponse` whose body is a stream of SSE events.
Each token chunk MUST be emitted as one SSE event
`data: <token>\n\n`, terminated by `data: [DONE]\n\n` when the agent
finishes.

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

### Requirement: Static Assets Served With Cache-Control

HTMX 1.9.10 MUST be vendored at `playground/static/htmx.min.js`
(Decision #1, no CDN). Templates MUST live in `playground/templates/`.
Static files MUST be served by Starlette's `StaticFiles` with
`Cache-Control: public, max-age=31536000, immutable` so the browser
caches the vendored HTMX across page loads.

#### Scenario: /static/htmx.min.js is reachable from the image

- GIVEN the runtime container is built per the `dockerfile-playground`
  spec
- WHEN a browser sends `GET /static/htmx.min.js`
- THEN the response MUST be 200
- AND the body MUST contain the literal string `1.9.10` (the version
  marker embedded in the minified HTMX 1.9.10 file; the upstream
  `/* htmx.org */` banner is stripped in the production min build)
- AND the body MUST be > 10 KB (HTMX 1.9.10 is 47,755 bytes
  uncompressed; REL-6 corrects the earlier ~14 KB figure — that was
  the gzipped wire size)
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
eight base tones (`--solar-base03` … `--solar-base3`) and the eight accent
colors (`--solar-yellow`, `--solar-orange`, `--solar-red`,
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
  chat page MUST show "JavaScript required" notice if `EventSource` is
  not supported.

## Test Scenarios

| Scenario | Required because |
|---|---|
| `GET /`, `GET /playground`, `GET /chat` all return 200 with `text/html` | Page surface contract |
| Each `/playground/api/{tool_name}` returns a fragment in < 500 ms (mock LLM) | Latency budget |
| `/chat/stream` delivers ≥ 2 chunks within 5 s in mock mode | SSE smoke (Decision #5) |
| `GET /static/htmx.min.js` is reachable from inside the container | Docker build correctness |
| Hexagonal invariant test stays GREEN (no new imports from `interfaces/` into `application/`) | Architecture invariant |
| Playwright smoke (gated `@pytest.mark.e2e`) loads `/playground`, submits one form, asserts the result `<div>` swaps | End-to-end happy path |
