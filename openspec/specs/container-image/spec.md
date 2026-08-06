# container-image

## Purpose

The multi-stage Dockerfile that produces a deployable image for Fly.io, Hugging Face Spaces, or Render from the same source. It bakes the vector index at build time (so the runtime has zero cold-start work), runs as a non-root user, exposes a `/healthz` healthcheck, and stays under 500 MB. The `$GEMINI_API_KEY` MUST be passed via BuildKit `--secret` so it never lands in the image layers.

> **Note on size budget (change 003-playground-ui)**: The original target was
> < 150 MB. The realistic budget for this Python+AI stack on
> `python:3.10.12-slim` (with LangGraph, sqlite-vec, FastMCP, etc.) is
> ~500 MB. The invariant was relaxed in `openspec/config.yaml` for this
> reason; an Alpine migration (deferred) drives the budget back toward
> 150 MB.

## Schema / Interface

```dockerfile
# Stage 1 — builder
#   - Installs build-essential, python -m venv /opt/venv, pip install --no-cache-dir .
#   - Installs gitleaks Go binary (tarball) into /usr/local/bin
#   - Runs `python -m mcp_server.interfaces.cli.preindex` with --secret id=gemini,env=GEMINI_API_KEY

# Stage 2 — runtime
#   - python:3.10.12-slim base
#   - Non-root user mcp with UID 10001 / GID 10001
#   - COPY --from=builder /opt/venv
#   - COPY --from=builder /app/data/index.sqlite  (baked index)
#   - EXPOSE $PORT (default 8080)
#   - HEALTHCHECK via httpx probe of /healthz
#   - CMD ["uvicorn", "mcp_server.app:app", "--host", "0.0.0.0", "--port", "$PORT", "--workers", "1"]
```

## Requirements

### Requirement: Multi-Stage Build Bakes the Index

The Dockerfile MUST have a `builder` stage that runs `preindex` with `GEMINI_API_KEY` injected via BuildKit `--secret`, and a `runtime` stage that copies the resulting `data/index.sqlite` from the builder. The runtime image MUST NOT contain any BuildKit secret mounts.

#### Scenario: Index baked at build time

- GIVEN `docker build --secret id=gemini,env=GEMINI_API_KEY -t mcp-server:test .`
- WHEN the build completes
- THEN `data/index.sqlite` MUST exist in the runtime image (verifiable with `docker run --rm mcp-server:test ls -la /app/data/`)
- AND the image MUST NOT contain any reference to the API key (verifiable with `docker history mcp-server:test` and `docker run --rm mcp-server:test env | grep -i gemini`).

#### Scenario: Build without GEMINI_API_KEY still succeeds

- GIVEN the build is invoked without `--secret id=gemini`
- WHEN the builder enters the preindex stage
- THEN `preindex` MUST detect the missing key and skip with a warning (`echo "WARN: preindex skipped (no API key)"`)
- AND the build MUST still produce an image
- AND the image MUST start `uvicorn` normally (it will return empty results from the in-memory index).

#### Scenario: gitleaks binary present in builder

- GIVEN the builder stage is built
- WHEN `docker run --rm --entrypoint gitleaks mcp-server:test version` is invoked against the builder stage
- THEN gitleaks MUST respond with its version
- AND the runtime stage MUST NOT contain the gitleaks binary (smaller image).

### Requirement: Non-Root Runtime User

The runtime image MUST run as a non-root user. The user MUST be named `mcp`, MUST have UID `10001`, and MUST own `/app` and all files under it.

#### Scenario: Container runs as non-root

- GIVEN the runtime image is running
- WHEN `docker exec <container> id -u` is invoked
- THEN the output MUST be `10001`
- AND `id -un` MUST be `mcp`.

#### Scenario: Read-only filesystem safety

- GIVEN the runtime container is running as `mcp`
- WHEN the container tries to write to `/etc` or any non-`/app` path
- THEN it MUST fail with `PermissionError`
- AND `/app` MUST be writable.

### Requirement: HEALTHCHECK Probes /healthz

The runtime image MUST declare a `HEALTHCHECK` that pings `http://localhost:$PORT/healthz` via httpx and exits non-zero on non-200.

#### Scenario: Container reports healthy

- GIVEN the container is running and `uvicorn` is up
- WHEN Docker probes the healthcheck
- THEN the probe MUST return `healthy`.

#### Scenario: Container reports unhealthy when app is down

- GIVEN the container is running but `uvicorn` is killed (simulated)
- WHEN Docker probes the healthcheck
- THEN the probe MUST return `unhealthy` after the configured number of retries.

### Requirement: EXPOSE and CMD

The runtime image MUST `EXPOSE $PORT` and MUST `CMD ["uvicorn", "mcp_server.app:app", "--host", "0.0.0.0", "--port", "$PORT", "--workers", "1"]`. The `--workers 1` choice keeps the 256 MB Fly.io machine happy.

#### Scenario: CMD binds to the platform port

- GIVEN `PORT=7860` (Hugging Face Spaces default)
- WHEN the container starts
- THEN uvicorn MUST bind to `0.0.0.0:7860`
- AND `docker run --rm -p 7860:7860 -e PORT=7860 mcp-server:test` MUST work without rebuild.

#### Scenario: EXPOSE reflects $PORT

- GIVEN the image is built with `PORT=10000` (Render default)
- WHEN `docker inspect mcp-server:test --format '{{json .Config.ExposedPorts}}'` is invoked
- THEN `10000/tcp` MUST appear in the exposed ports list.

### Requirement: Final Image Size < 500 MB

The runtime image MUST have a compressed size under 500 MB.

> Original target was < 150 MB; relaxed per change 003-playground-ui
> because the realistic budget for the Python+AI stack is ~500 MB.

#### Scenario: Image size is below the budget

- GIVEN the image is built
- WHEN `docker image ls mcp-server:test --format '{{.Size}}'` is invoked
- THEN the reported size MUST be < 500 MB (≈ 524,288,000 bytes).

#### Scenario: No build-essentials in runtime

- GIVEN the runtime image
- WHEN `docker run --rm mcp-server:test dpkg -l build-essential` is invoked
- THEN it MUST exit non-zero (package not installed).

## Error / Edge Cases

- BuildKit unavailable (Docker < 23.0): the build MUST fail with a clear error pointing the user to enable BuildKit or upgrade Docker.
- `pyproject.toml` missing or invalid: the build MUST fail at the `pip install` step.
- `data/index.sqlite` not produced in builder (e.g. preindex crashed): the runtime stage MUST still build, but the container MUST log a warning at startup and `/healthz` MUST still return 200 (the index is a soft dependency for tools, not for the probe).
- Docker registry does not preserve BuildKit secrets: this is the user's responsibility; the spec only guarantees the secrets are NOT in the published image.

## Test Scenarios

| Scenario | Required because |
|---|---|
| Built image exposes `$PORT` and binds uvicorn to it | Port-agnostic deploy |
| Built image runs as UID 10001 | Non-root security |
| `GEMINI_API_KEY` does not appear in `docker history` or `env` | Secret hygiene |
| `gitleaks detect --redact` against the built image finds no secrets | CI gate |
| Final image size < 500 MB | Cost discipline (relaxed from < 150 MB per 003-playground-ui) |
| `--mock-gemini` build produces a working image with an empty index | Testability / CI |
