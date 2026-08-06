"""FastAPI app factory — the composition root entry point.

``create_app()`` is the only function that wires the composition root,
the ``/healthz`` route, and the FastMCP sub-app mount at ``/mcp``. It is
the application factory referenced by ``pyproject.toml``'s
``[project.scripts] mcp-server = "mcp_server.app:run"`` console script.

Wiring pattern (per FastMCP 3.2.4+ docs):

    mcp_app = mcp.http_app(path="/")
    app = FastAPI(lifespan=mcp_app.lifespan)
    app.mount("/mcp", mcp_app)

The parent FastAPI MUST share the MCP lifespan so the sub-app initializes
its session manager on startup. ``FastMCP.mount(app, path)`` does NOT
exist on this version — only the ``app.mount(...)`` direction works.

``run()`` is a thin wrapper over :func:`uvicorn.run` that binds to
``0.0.0.0:$PORT`` with ``--workers 1``. Workers=1 is mandatory for the
slowapi in-memory rate limiter wired in PR2 — see
``openspec/changes/001-bootstrap/design/adrs/001-composition-eager-vs-lazy.md``.
"""

from __future__ import annotations

from fastapi import FastAPI

from mcp_server.composition import create_composition
from mcp_server.config import AppConfig, load_config
from mcp_server.interfaces.http.healthz import build_healthz_router
from mcp_server.interfaces.http.middleware.sanitizer import (
    OutputSanitizerMiddleware,
)
from mcp_server.interfaces.http.web import build_web_router
from mcp_server.interfaces.mcp.server import mcp


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build a configured FastAPI application.

    Steps:

    1. Load config from env vars (or accept an injected config for tests).
    2. Build the FastMCP sub-app via ``mcp.http_app(path="/")``.
    3. Construct the parent FastAPI with the MCP lifespan.
    4. Wire the composition root into ``app.state.composition``.
    5. Mount ``/healthz`` via the healthz router.
    6. Mount the MCP sub-app at ``/mcp``.
    7. Register the OutputSanitizerMiddleware (Layer 3 — task 2.13).

    Each call returns a fresh, independently usable FastAPI instance —
    there is no module-level cache beyond the optional ``app`` constant
    exported for uvicorn's import-string convention.
    """
    if config is None:
        config = load_config()

    # FastMCP 3.2.4+ mount pattern. The parent FastAPI MUST share the MCP
    # lifespan so the sub-app initializes its session manager on startup.
    mcp_app = mcp.http_app(path="/")

    # Eagerly wire composition so we can install the middleware with
    # the sanitizer instance it owns. We register the middleware via
    # ``add_middleware`` AFTER the app is built so Starlette's
    # ``BaseHTTPMiddleware`` picks up the FastAPI lifespan.
    composition = create_composition(config)

    app = FastAPI(
        title="mcp-server-playground",
        lifespan=mcp_app.lifespan,
    )

    # Composition root — single wiring point (ADR-001).
    app.state.composition = composition

    # Layer 3 — OutputSanitizerMiddleware rewrites every response
    # body to redact secrets before they leave the server. Skips
    # /healthz and /mcp (see middleware/sanitizer.py).
    app.add_middleware(
        OutputSanitizerMiddleware,
        sanitizer=composition.sanitizer,
    )

    # Operational probe.
    app.include_router(build_healthz_router())

    # Browser-facing playground router (forms + landing + static).
    # Mounted between /healthz and the /mcp sub-app per
    # change 003-playground-ui (playground-ui spec schema).
    app.include_router(build_web_router())

    # MCP sub-app mount.
    app.mount("/mcp", mcp_app)

    return app


def run() -> None:
    """Entry point for ``mcp-server`` console script.

    Reads ``$PORT`` from config (the single source of env). Binds uvicorn
    to ``0.0.0.0`` with ``workers=1`` (mandatory for slowapi in-memory
    state once PR2 wires the rate limiter).
    """
    import uvicorn

    config = load_config()
    # S104: 0.0.0.0 binding is intentional — the container serves on all
    # interfaces and is fronted by the platform proxy (Fly.io, HF Spaces,
    # Render, etc.). The Dockerfile healthcheck and MCP clients connect
    # through the platform's port mapping, not directly to localhost.
    uvicorn.run(
        "mcp_server.app:app",
        host="0.0.0.0",  # noqa: S104
        port=config.port,
        workers=1,
    )


#: Module-level app for ``uvicorn mcp_server.app:app`` and ``mcp-server``
#: console script. ``create_app()`` is the proper factory for tests.
app: FastAPI = create_app()


__all__ = ["app", "create_app", "run"]
