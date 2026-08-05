"""FastAPI app factory — the composition root entry point.

``create_app()`` is the only function that wires the composition root,
the ``/healthz`` route, and (in change after this PR) the FastMCP sub-app
mount at ``/mcp``. It is the application factory referenced by
``pyproject.toml``'s ``[project.scripts] mcp-server = "mcp_server.app:run"``
console script.

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


def create_app(config: AppConfig | None = None) -> FastAPI:
    """Build a configured FastAPI application.

    Steps:

    1. Load config from env vars (or accept an injected config for tests).
    2. Construct the parent FastAPI.
    3. Wire the composition root into ``app.state.composition``.
    4. Mount ``/healthz`` via the healthz router.

    Each call returns a fresh, independently usable FastAPI instance —
    there is no module-level cache beyond the optional ``app`` constant
    exported for uvicorn's import-string convention.
    """
    if config is None:
        config = load_config()

    app = FastAPI(title="mcp-server-playground")

    # Composition root — single wiring point (ADR-001).
    app.state.composition = create_composition(config)

    # Operational probe.
    app.include_router(build_healthz_router())

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
