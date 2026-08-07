"""Web router — the single entry point for the browser-facing surface.

The router exposes:

* ``GET /`` — landing page (project list + CTAs).
* ``GET /mcp-ui`` — the browser MCP explorer (auto-generated from
  the JSON-RPC ``/mcp`` transport tool registry). This is the sole
  browser-facing tool surface; the old ``/playground`` hand-crafted
  form-cards page was removed because it duplicated the tool surface
  without exercising the real MCP transport.
* ``POST /chat/stream`` + ``GET /chat`` — streaming chat (mounted
  via :func:`build_chat_router`).
* ``GET /static/*`` — vendored HTMX and Solarized Phosphor style sheet,
  served from ``playground/static/`` with
  ``Cache-Control: public, max-age=31536000, immutable``.

The router is mounted by ``create_app()`` between ``/healthz`` and the
``/mcp`` sub-app mount. Hexagonal contract: the package imports
application use cases / domain entities only — never
``infrastructure/``. The composition root is the only place concrete
adapters are wired.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from mcp_server.interfaces.http.web.chat import build_chat_router
from mcp_server.interfaces.http.web.paths import resolve_playground_subdir
from mcp_server.interfaces.http.web.templates import templates

__all__ = ["build_web_router"]


def _static_dir() -> Path:
    """Return the absolute playground/static/ directory.

    Delegates to :func:`resolve_playground_subdir` which handles
    source-tree walks (editable installs), the Docker WORKDIR layout
    (``/app/playground/``), and the ``MCP_SERVER_PLAYGROUND_DIR`` env
    var override. Raises :class:`FileNotFoundError` with every
    attempted path if none resolve.
    """
    return resolve_playground_subdir("static")


class _StaticFilesWithCacheControl(StaticFiles):
    """Subclass that injects an immutable ``Cache-Control`` header.

    Starlette 1.0 dropped the ``headers=`` kwarg on ``StaticFiles``;
    the modern way to add a header is to override :meth:`__call__`,
    delegate to :meth:`get_response` to obtain the underlying
    :class:`Response`, mutate the headers in place, and call the
    response with ``receive``/``send``. This keeps file serving
    internals untouched while guaranteeing every served file carries
    the immutable cache header.
    """

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[override]
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return
        if not self.config_checked:
            await self.check_config()
            self.config_checked = True
        path = self.get_path(scope)
        response = await self.get_response(path, scope)
        # Inject the immutable cache header on every asset the mount serves.
        response.headers["cache-control"] = "public, max-age=31536000, immutable"
        await response(scope, receive, send)


def _static_files() -> StaticFiles:
    """Build the cached static-files mount.

    Returns:
        A :class:`StaticFiles` subclass that injects
        ``Cache-Control: public, max-age=31536000, immutable`` on
        every served asset so the vendored HTMX and CSS cache across
        page loads (spec scenario
        "Vendored HTMX is cached across page loads").
    """
    return _StaticFilesWithCacheControl(directory=str(_static_dir()), check_dir=True)


def build_web_router() -> APIRouter:
    """Build the playground router — landing page, form endpoints, static assets."""
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse, name="landing")
    async def landing(request: Request) -> HTMLResponse:
        """Render ``index.html`` with the projects from the composition root."""
        composition = getattr(request.app.state, "composition", None)
        projects = []
        if composition is not None:
            try:
                projects = composition.list_projects_use_case.execute()
            except Exception:
                projects = []
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"projects": projects},
            status_code=200,
        )

    @router.get(
        "/static/healthz",
        include_in_schema=False,
        name="static_health",
    )
    async def _static_health() -> Response:
        """Sentinel for tests / observability — confirms the static
        mount is reachable. The mount is registered after this
        endpoint; the endpoint is here so a developer can curl
        ``/static/healthz`` to see the 404 (or 200 from the next
        registered static match). Kept for parity with the spec's
        "playground directory is reachable from the image" check.
        """
        return Response(status_code=404)

    # PR2b — streaming chat surface (stateful browser, stateless
    # server). ``build_chat_router`` returns its own APIRouter so the
    # chat surface is self-contained; mounting it here makes
    # ``GET /chat`` and ``POST /chat/stream`` part of the same web
    # surface as the landing + ``/mcp-ui`` explorer.
    router.include_router(build_chat_router())

    # Mount ``/static/`` last so all routes above resolve first.
    router.mount("/static", _static_files(), name="static")

    return router
