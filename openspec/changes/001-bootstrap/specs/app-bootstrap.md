# app-bootstrap

## Purpose

The FastAPI application factory that bootstraps the runtime server. This is the **composition root**: the only entry point that wires concrete adapters to use cases, exposes a `/healthz` probe, mounts the FastMCP sub-app, and binds to a port supplied by the platform. Without it, the rest of the system has no surface to start.

## Schema / Interface

```python
# src/mcp_server/app.py
from fastapi import FastAPI

def create_app() -> FastAPI:
    """MUST be the only entry point that constructs the FastAPI app."""

def run() -> None:
    """MUST read $PORT from the environment and start uvicorn with --workers 1."""

# src/mcp_server/config.py — the ONLY module allowed to call os.environ
from pydantic import BaseModel

class AppConfig(BaseModel):
    port: int                  # from $PORT, default 8080
    log_level: str             # from $LOG_LEVEL, default "INFO"
    manifest_path: str         # from $MANIFEST_PATH, default "config/projects.manifest.yaml"
    gemini_api_key: str | None # from $GEMINI_API_KEY (set at build time only)
    embedding_dim: int         # from $EMBEDDING_DIM, default 768

def load_config() -> AppConfig: ...

# src/mcp_server/build_info.py — version metadata returned by /healthz
class BuildInfo(BaseModel):
    version: str       # from pyproject.toml
    commit_sha: str    # from $COMMIT_SHA or "unknown"
    built_at: str      # ISO-8601 from $BUILT_AT or "unknown"
```

## Requirements

### Requirement: App Factory

The system MUST expose a `create_app()` factory in `src/mcp_server/app.py`. `create_app()` MUST call `composition.compose()` to obtain the wired container and MUST register only the health, MCP, and (in later changes) HTTP routes — no business logic may live in `app.py`.

#### Scenario: Build an app instance

- GIVEN the package is installed (`pip install -e ".[dev]"`)
- WHEN `create_app()` is called
- THEN it MUST return a `FastAPI` instance
- AND `app.title` MUST equal `"mcp-server-playground"`
- AND the compositio root MUST have been invoked exactly once.

#### Scenario: Composition root is the only wiring point

- GIVEN a fresh app instance
- WHEN the import graph is inspected
- THEN `src/mcp_server/interfaces/http/` and `src/mcp_server/interfaces/mcp/` MUST NOT import from `src/mcp_server/infrastructure/adapters/`
- AND `composition.py` MUST be the only module that imports both an adapter and a use case.

### Requirement: Healthz Endpoint

The system MUST serve `GET /healthz` returning HTTP 200 with JSON body `{status, version, commit_sha, built_at}`.

#### Scenario: Healthz returns 200 with version info

- GIVEN the app is running and `BuildInfo` is populated
- WHEN a client sends `GET /healthz`
- THEN the response status MUST be 200
- AND the body SHALL contain `status: "ok"`, `version`, `commit_sha`, and `built_at`.

#### Scenario: Healthz with missing build metadata

- GIVEN `COMMIT_SHA` and `BUILT_AT` env vars are unset
- WHEN a client sends `GET /healthz`
- THEN the response MUST still be 200
- AND `commit_sha` and `built_at` MUST both be the string `"unknown"`.

#### Scenario: Healthz output passes sanitization

- GIVEN `OutputSanitizer` is registered as a response middleware
- WHEN `/healthz` is invoked and one of the build env vars happens to contain a token-like substring (e.g. `commit_sha=ghp_abc123…`)
- THEN any token-shaped substring in the JSON body MUST be replaced with `[REDACTED]`
- AND an incident MUST be appended to the audit log (Layer 3 / Layer 5 stack).

### Requirement: Port-Agnostic Binding

The system MUST bind to the port supplied by the `$PORT` environment variable so the same image runs on Fly.io (8080), Hugging Face Spaces (7860), and Render (10000) without rebuild.

#### Scenario: Default port from $PORT

- GIVEN `PORT=8080` is set in the environment
- WHEN `run()` is invoked
- THEN uvicorn MUST bind to `0.0.0.0:8080`
- AND `--workers 1` MUST be passed.

#### Scenario: Unset PORT falls back to default

- GIVEN `PORT` is unset
- WHEN `run()` is invoked
- THEN the bound port MUST be `8080`
- AND a warning SHALL be logged: `"$PORT unset, defaulting to 8080"`.

#### Scenario: Invalid PORT value rejected

- GIVEN `PORT=abc`
- WHEN `load_config()` is called
- THEN it MUST raise `pydantic.ValidationError`
- AND `app` MUST fail to start (non-zero exit).

### Requirement: MCP Sub-App Mount

The system MUST mount the FastMCP server at `/mcp` so MCP clients reach it under the same host and port.

#### Scenario: MCP sub-app reachable at /mcp

- GIVEN the app is running
- WHEN a client sends `GET /mcp` (or performs the MCP initialize handshake)
- THEN the request MUST be routed to the FastMCP sub-app
- AND no request outside `/mcp` SHALL be handled by the sub-app.

## Error / Edge Cases

- `create_app()` MUST be idempotent — calling it twice MUST produce two independently usable apps (no module-level singletons beyond the config).
- DB path collisions during local dev: if `data/index.sqlite` is missing, the app MUST start anyway and answer `/healthz` with 200; only `/mcp` tool calls surface the missing index.
- `load_config()` MUST never silently coerce — invalid numeric/boolean values raise `ValidationError` immediately.

## Test Scenarios

| Scenario | Required because |
|---|---|
| `create_app` returns FastAPI with expected title | App factory contract |
| `/healthz` returns 200 + version JSON | Operational probe |
| `/healthz` redacts token-shaped substrings via OutputSanitizer | **Layer 3** requirement |
| `run()` reads `$PORT` for uvicorn binding | Port-agnostic deploy |
| Composition root is the only wiring point (import-graph test) | Hexagonal invariant |
