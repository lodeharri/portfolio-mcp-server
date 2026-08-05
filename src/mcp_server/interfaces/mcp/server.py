"""FastMCP sub-app factory — the ``/mcp`` mount source.

This module exposes the single :class:`FastMCP` instance used by the
composition root. Real ``@mcp.tool`` registrations land in change
``002-mcp-tools``; PR1 ships an empty server so the FastAPI factory can
mount the sub-app at ``/mcp`` and the MCP initialize handshake succeeds.

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
#: in change ``002-mcp-tools``. The instance name is reused as the
#: ``server.name`` field in the MCP initialize response.
mcp: FastMCP = FastMCP("mcp-server-playground")


__all__ = ["mcp"]
