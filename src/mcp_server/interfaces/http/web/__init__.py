"""Browser-facing HTTP package — the landing + MCP explorer surface.

Module map:

* ``deps`` — :func:`get_composition`, the helper the routes use to
  look up :class:`mcp_server.composition.Composition` from
  ``request.app.state.composition``.
* ``templates`` — single shared :class:`fastapi.templating.Jinja2Templates`
  bound to ``playground/templates/`` so every page renders through the
  same environment.
* ``router`` — :func:`build_web_router` returning an :class:`APIRouter`
  with the static mount, ``GET /`` (landing), ``GET /mcp-ui``
  (auto-generated browser MCP explorer), and the chat surface
  (``GET /chat`` + ``POST /chat/stream``).
* ``mcp_browser`` — the ``/mcp-ui`` page, generated from the real
  JSON-RPC ``/mcp`` transport tool registry.

The package imports application ports / use cases / security layers
ONLY — never ``infrastructure/`` (hexagonal invariant under the
``interfaces/`` boundary).
"""

from mcp_server.interfaces.http.web.mcp_browser import build_mcp_browser_router
from mcp_server.interfaces.http.web.router import build_web_router

__all__ = ["build_mcp_browser_router", "build_web_router"]
