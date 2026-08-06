# dockerfile-playground — Delta Specification

## Purpose

The container-image delta that ships the playground assets inside the
runtime image. The current `Dockerfile` (post-`005-langchain-integration`,
417 MB runtime image) does NOT include the `playground/` directory; the
runtime image must carry Jinja2 templates, the vendored HTMX 1.9.10, and
`static/style.css` so the routes added by this change have something to
serve. Per Decision #10 the vendored HTMX is committed to the repo so
CI / image builds do not depend on a CDN fetch.

Per Risk #5 in the proposal, missing the `playground/` COPY would mean
`/static/htmx.min.js` 404s inside the production container even though the
file exists on the build host.

## Schema / Interface

```dockerfile
# Dockerfile — new line after the existing scripts COPY (REL-13 amendment:
# relative position; absolute line numbers drift as the file evolves)
COPY --chown=mcp:mcp playground/ ./playground/
```

The line MUST be inserted between the existing `COPY scripts ./scripts`
and the venv creation (`RUN python -m venv /opt/venv`). It MUST appear
BEFORE the venv creation so the build can cache the `playground/` layer
independently of the (slower) pip install layer.

> REL-13 amendment: previous revisions of this spec referenced absolute
> line numbers (line 76, line 81, line 125). Those are lies waiting to
> happen — the Dockerfile evolves, and the structural test that pins
> the COPY position is robust to file edits. The relative-position
> assertion above is the canonical contract; absolute numbers have been
> removed.

## MODIFIED Requirements

### Requirement: Playground Assets Shipped in the Runtime Image

The runtime stage of the Dockerfile MUST contain the entire `playground/`
directory at `/app/playground/` with the non-root user `mcp` as owner. The
image MUST contain at minimum: `playground/templates/base.html`,
`playground/templates/index.html`, `playground/templates/playground.html`,
`playground/templates/chat.html` (and partials), and
`playground/static/htmx.min.js` plus `playground/static/style.css`.

(Previously: the runtime image did not include `playground/`; the
`create_app()` factory had no templates or vendored HTMX to serve.)

#### Scenario: Runtime image contains the templates and HTMX

- GIVEN `docker build -t mcp-server:test .` completes
- WHEN `docker run --rm mcp-server:test ls -la /app/playground/` runs
- THEN the directory MUST exist
- AND `playground/templates/` MUST contain `base.html`
- AND `playground/static/` MUST contain `htmx.min.js`
- AND `playground/static/` MUST contain `style.css`.

#### Scenario: htmx.min.js is reachable from inside the container

- GIVEN the runtime container is running and uvicorn is up
- WHEN a browser sends `GET /static/htmx.min.js` against the container
- THEN the response MUST be 200
- AND the body MUST contain the literal string `1.9.10` (the version
  marker embedded in the minified HTMX 1.9.10 file; the upstream
  `/* htmx.org */` banner is stripped in the production min build)
- AND the response MUST include
  `Cache-Control: public, max-age=31536000, immutable`.

#### Scenario: Playground dir ownership matches non-root user

- GIVEN the runtime image is built
- WHEN `docker run --rm mcp-server:test stat -c '%U:%G' /app/playground`
  runs
- THEN the output MUST equal `mcp:mcp`
- AND `docker exec <container> id -un` MUST still equal `mcp`.

#### Scenario: Playground COPY is placed before venv creation

- GIVEN the Dockerfile is built
- WHEN the COPY / RUN sequence is inspected
- THEN the `COPY --chown=mcp:mcp playground/ ./playground/` line MUST
  appear after the existing `COPY scripts ./scripts` step and BEFORE
  the `RUN python -m venv /opt/venv` step (REL-13 amendment —
  relative position assertion; absolute line numbers removed)
- AND the runtime stage MUST contain a corresponding
  `COPY --from=builder /app/playground ./playground` (or equivalent
  builder-to-runtime propagation) so the assets are available to
  uvicorn at request time.

> **Note on stage separation (REL-13):** the proposal adds the COPY in
> the **builder** stage (after `COPY scripts`, before `RUN python -m venv`),
> then propagates `playground/` from the builder to the runtime stage
> via `COPY --from=builder`. The structural test that pins this
> ordering is robust to file edits; absolute line numbers were
> removed because they lie as the Dockerfile evolves.

### Requirement: Final Image Size Stays Under 500 MB

The runtime image MUST have a compressed size under 500 MB. The current
baseline (post-005) is 417 MB; the playground additions are < 1 MB total
(HTMX 1.9.10 = 47,755 bytes / ~48 KB uncompressed — REL-6 corrects the
earlier ~14 KB figure; Jinja2 templates ~20 KB; CSS ~6 KB; total
playground delta ~75 KB). This delta therefore has a budget of ~83 MB
headroom before any other change.

(Previously: image size budget was `< 150 MB` per the original
`container-image` spec; the proposal relaxes the budget to `< 500 MB` to
keep headroom for future additions. The change is documented here for
consistency with the cost-discipline narrative.)

#### Scenario: Built image size is below the 500 MB budget

- GIVEN `docker build -t mcp-server:test .` completes
- WHEN `docker image ls mcp-server:test --format '{{.Size}}'` runs
- THEN the reported size MUST be < 500 MB (≈ 524,288,000 bytes)
- AND MUST be ≤ 425 MB (current 417 MB baseline + 8 MB safety margin)
  so the budget shrinks back toward 150 MB as future changes land.

#### Scenario: Vendored HTMX is in the image (no CDN fallback)

- GIVEN the runtime image is running
- WHEN `docker exec <container> cat /app/playground/static/htmx.min.js`
  runs
- THEN the command MUST exit 0
- AND the file MUST be > 10 KB (HTMX 1.9.10 is 47,755 bytes / ~48 KB
  uncompressed; REL-6 corrects the earlier ~14 KB figure — that was the
  gzipped wire size)
- AND the image MUST NOT contain any reference to a CDN URL for HTMX
  (grep the image layers for `unpkg.com`, `cdn.jsdelivr.net`, etc. —
  no matches).

### Requirement: Fly.io Autoscale-to-Zero Stays Configured

`fly.toml` requires NO changes for the new routes. The existing
`auto_stop_machines = "stop"` and `min_machines_running = 0` settings
already autoscale to zero; SSE concurrency under the
`hard_limit = 50` (HTTP service `concurrency` setting in `fly.toml`)
is sufficient for the demo (≤ 50 simultaneous recruiters per machine).

(Previously: not declared in this spec; added as a delta to make the
deploy-cost claim in the proposal auditable.)

#### Scenario: fly.toml is unchanged

- GIVEN `git diff main -- fly.toml` after this change lands
- WHEN the diff is inspected
- THEN the file MUST NOT be modified
- AND `auto_stop_machines = "stop"` MUST remain set
- AND `min_machines_running = 0` MUST remain set.

#### Scenario: SSE concurrency fits within Fly.io limits

- GIVEN 50 simultaneous recruiters hit `/chat/stream` on one machine
- WHEN the SSE connections are counted
- THEN the count MUST NOT exceed the `hard_limit` configured in
  `fly.toml`
- AND no recruiter MUST receive a 503 from Fly's edge
- AND autoscale-to-zero MUST still apply after all 50 connections close.

## Error / Edge Cases

- If `playground/static/htmx.min.js` is missing from the build context
  (developer forgot to vendor it), the build MUST NOT silently succeed
  — the integration test `tests/integration/test_docker_size.py` MUST
  fail with a clear "HTMX not vendored" message.
- The `playground/` COPY MUST NOT use `--chmod` flags that override
  `--chown`; the non-root `mcp` user MUST own every file under
  `/app/playground/`.
- A future addition that increases the image size past 425 MB MUST
  trigger a review (cost discipline: every byte costs on Fly.io).

## Test Scenarios

| Scenario | Required because |
|---|---|
| Runtime image contains `/app/playground/static/htmx.min.js` | Vendored-HTMX invariant (Decision #1) |
| Runtime image size < 500 MB (current baseline 417 MB) | Cost discipline |
| `playground/` COPY placed after `COPY scripts` and before `python -m venv` (REL-13: relative position) | Build cache hygiene |
| `fly.toml` is unchanged in this PR | Deploy config stability |
| Integration test asserts `htmx.min.js` is reachable from inside the container | End-to-end build correctness |
