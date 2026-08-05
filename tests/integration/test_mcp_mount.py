"""Integration tests for the FastMCP sub-app mount at ``/mcp``.

These tests are RED until ``src/mcp_server/interfaces/mcp/server.py`` and
the ``app.mount("/mcp", ...)`` wiring in ``src/mcp_server/app.py`` are
implemented.

The FastMCP 3.2.4+ mount pattern (per design.md) is::

    mcp_app = mcp.http_app(path="/")
    app = FastAPI(lifespan=mcp_app.lifespan)
    app.mount("/mcp", mcp_app)

NOT ``FastMCP.mount(app, path)`` — that method does not exist on FastMCP.
"""

from __future__ import annotations

import httpx

from mcp_server.app import create_app


def _make_client() -> httpx.AsyncClient:
    """Build an async httpx client wired to ``create_app()`` via ASGI transport."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


class TestMcpMount:
    async def test_mcp_route_is_registered(self) -> None:
        """``GET /mcp`` must NOT return 404 — the MCP sub-app handles it."""
        async with _make_client() as client:
            response = await client.get("/mcp")
        assert response.status_code != 404, (
            f"Expected /mcp to be handled by the FastMCP sub-app, got {response.status_code}"
        )

    async def test_mcp_route_does_not_shadow_healthz(self) -> None:
        """``/healthz`` must still respond normally — the mount is path-scoped."""
        async with _make_client() as client:
            healthz = await client.get("/healthz")
            mcp = await client.get("/mcp")
        assert healthz.status_code == 200
        assert mcp.status_code != 404
        assert healthz.json()["status"] == "ok"
