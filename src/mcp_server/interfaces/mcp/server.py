"""FastMCP sub-app factory — the ``/mcp`` mount source.

This module exposes the single :class:`FastMCP` instance used by the
composition root. Tool registrations live in ``tools.py`` and are
fired by the side-effect import at the bottom of this file.

Mount pattern (FastMCP 3.2.4+ — verified against 3.4.6)::

    mcp_app = mcp.http_app(path="/")
    app = FastAPI(lifespan=mcp_app.lifespan)
    app.mount("/mcp", mcp_app)

``FastMCP.mount(app, path)`` does NOT exist on this version — only the
``app.mount(...)`` direction is supported.
"""

from __future__ import annotations

from fastmcp import FastMCP

#: The single FastMCP server instance. Tools register via ``@mcp.tool``
#: in :mod:`mcp_server.interfaces.mcp.tools` (imported below). The
#: instance name is reused as the ``server.name`` field in the MCP
#: initialize response.
mcp: FastMCP = FastMCP("mcp-server-playground")


# Side-effect import: registering @mcp.tool decorators MUST happen at
# module-load time so the FastMCP server's tool registry is populated
# before the first request. Importing the module executes the
# decorators; the composition root later populates the use case
# container via ``tools.set_use_cases(...)``.
from mcp_server.interfaces.mcp import tools as _tools  # noqa: E402, F401


__all__ = ["mcp"]
