# ADR 001: HTMX 1.9.10 vendored — not React, Vue, or a SPA framework

- **Status**: Accepted
- **Date**: 2026-08-06
- **Change**: `003-playground-ui`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

The playground needs to render five forms (one per non-agent MCP tool) and swap their results without a full page reload, and it needs to render a chat transcript that updates as SSE tokens arrive. Three product positions must hold simultaneously:

1. **Recruiter demo must "just work"** — no build step, no `npm install`, no Playwright pre-installed by the visitor, no CDN dependency.
2. **Hexagonal discipline** — the templates are inputs to the HTTP adapter, not application code; no JSX, no SSR complexity, no Node toolchain inside the Docker image.
3. **Image size budget** — the runtime image is < 500 MB and we want the playground delta to add < 1 MB total.

The natural alternatives split along two axes: server-rendered fragments vs client-rendered SPA, and vanilla JS vs framework. The HTMX vs SPA choice is the load-bearing one.

## Decision Drivers

- **D1**: Zero build step. No bundler, no transpiler, no separate Node process.
- **D2**: Server-rendered fragments fit the project's hexagonal rule (templates are HTTP adapter inputs, not application code).
- **D3**: Demo must work with no network on page load — no CDN, no `unpkg.com`, no `cdn.jsdelivr.net`. Sandbox environments and corporate networks block both.
- **D4**: Image size stays under 500 MB. A React SPA needs React + a router + a bundler runtime; that's already 200 KB minified before any app code.
- **D5**: Recruiter does not install anything. Click URL, see UI work.

## Considered Options

### Option A — HTMX 1.9.10 vendored at `playground/static/htmx.min.js` (chosen)

HTMX is a single 47,755-byte (~48 KB uncompressed, ~14 KB gzipped on the
wire per REL-6 amendment) JavaScript file that swaps HTML fragments into
the DOM in response to HTTP responses. The five form endpoints return
HTML fragments; HTMX swaps them into the per-tool
`<div id="result-{tool}">`. No JSX, no bundler, no Node. The file is
downloaded once from htmx.org, committed to the repo, and served by
Starlette's `StaticFiles` with
`Cache-Control: public, max-age=31536000, immutable`.

**Pros**:
- Single file, 47,755 bytes / ~48 KB uncompressed (~14 KB gzipped;
  REL-6 amendment), MIT-licensed, no dependencies.
- Server-rendered fragments — `Jinja2` (already in `pyproject.toml:30`) renders the HTML; HTMX just swaps it.
- Zero build step. The Docker image only needs Python + the vendored HTMX file.
- No CDN: deterministic builds, sandbox-friendly.
- Hexagonal purity: templates live under `playground/templates/`, which is an HTTP adapter input.

**Cons**:
- HTMX 1.9.x is feature-frozen (1.9.10 was the last 1.9 release; 2.x is a separate branch). For this change that's fine — we don't need 2.x features.
- HTMX 1.9.10 doesn't ship streaming SSE (the `htmx-sse` extension is a separate file). Chat uses native `EventSource` instead (ADR-004 confirms).
- Smaller talent pool familiar with HTMX than React. Mitigated by ~100 LOC total of HTMX-specific attributes (`hx-post`, `hx-target`, `hx-swap`); a developer can learn the surface in an afternoon.

### Option B — React + Vite SPA (rejected)

Build a React SPA that calls the same use cases via a JSON HTTP API. `npm run build` produces a static bundle; `playground/static/` serves it; the FastAPI backend exposes `JSONResponse` endpoints instead of HTML fragments.

**Pros**:
- Talent pool. Most developers know React.
- Rich client-side state (e.g., form state per card, chat scroll restoration, optimistic UI).

**Cons**:
- **Build step** — `npm install` + Vite adds 30+ seconds to every CI run and a 200 MB `node_modules` directory.
- **Image size** — `node:20-slim` is 250 MB on its own; we already use `python:3.10.12-slim`. Multi-stage builds mitigate but the final image still carries the static JS bundle, not just the JS source.
- **Hexagonal violation** — the SPA needs to know the use case JSON shape; that JSON shape leaks into the application layer or requires a parallel "DTO" layer.
- **Overkill** — five forms and one streaming chat do not need component composition, hooks, or state management.
- **CDN temptation** — most React tutorials use CDN ESM imports; CI must fight to keep them out.

### Option C — Vue 3 / Svelte / Alpine.js (rejected)

A middle-ground between vanilla and React. Each is smaller than React, but each still requires either a build step (Vue, Svelte) or a CDN dependency (Alpine).

**Pros**:
- Smaller than React.
- Svelte compiled output is tiny (~10 KB).
- Alpine.js is single-file, no build step.

**Cons**:
- Vue and Svelte require a build step → same issue as React.
- Alpine.js is a CDN-first project (`<script src="https://cdn.jsdelivr.net/npm/alpinejs">` is the canonical hello world). The no-CDN rule (D3) forces us to vendor Alpine and maintain the vendored copy manually.
- HTMX is purpose-built for "swap HTML fragments on form submit", which is exactly what the playground needs; Alpine is a general-purpose reactivity system.

## Decision

**Option A.** HTMX 1.9.10 vendored at `playground/static/htmx.min.js`. Five form endpoints return HTML fragments rendered by the shared `Jinja2Templates` instance. Chat uses native `EventSource` (ADR-004); HTMX is not used for the chat surface.

```html
<!-- playground/templates/playground.html (form card snippet) -->
<form hx-post="/playground/api/search_code"
      hx-target="#result-search_code"
      hx-swap="innerHTML">
  <input name="query" type="text" placeholder="rate limit" required>
  <button type="submit">Search</button>
</form>
<div id="result-search_code"></div>
```

The Docker image gains ~75 KB total uncompressed (HTMX 47,755 bytes +
CSS + templates + partials; ~14 KB of that is HTMX alone on the wire
post-gzip). No new dep in `pyproject.toml`.

## Consequences

**Positive**:
- Image delta is < 1 MB; well under the 500 MB budget.
- CI is faster (no Node step, no `npm install`).
- Hexagonal purity: `playground/templates/` is a pure HTTP adapter input.
- The vendored HTMX file is reproducible: CI builds the image identically regardless of network conditions.
- The `Cache-Control: immutable` header means the browser caches HTMX forever (one HTTP request total across a recruiter's session).

**Negative**:
- HTMX 1.9.10 is the last 1.9 release; future HTMX 2.x features (e.g., `hx-on:` for inline event handlers) are unavailable. Acceptable for a portfolio demo; documented as a future-migration path.
- Recruiter feedback that mentions "this is React" or "this is Vue" is no longer accurate — the playground is HTMX + Jinja2 + vanilla JS. The README and the recruiter demo script should mention this.
- If a future change wants React-style component composition, this design breaks down. The exit ramp is to refactor to React + Vite behind the same `build_web_router()` factory; the use cases don't move.

## Compliance with rules

- `rules.apply.guidelines` → "Hexagonal architecture is mandatory" — satisfied; templates are adapter inputs.
- `invariants` → "Single FastAPI process serves MCP + playground; no second service" — satisfied; HTMX runs in the browser, no Node process.
- `invariants` → "Container image final size <500 MB" (post-relaxation) — satisfied; playground delta is < 1 MB.

## Follow-ups

- In apply phase: write `tests/e2e/playground/test_smoke.py` asserting the vendored HTMX file is reachable at `/static/htmx.min.js` with the expected `Cache-Control` header.
- In verify phase: confirm `docker image ls mcp-server:test --format '{{.Size}}'` is < 500 MB.
- In archive phase: note the override of `openspec/config.yaml` rule "Streaming chat over HTMX uses htmx-ws (WebSocket), not Server-Sent Events" — see design.md Open Question #1.
