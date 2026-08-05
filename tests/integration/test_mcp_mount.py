"""Integration tests for the FastMCP sub-app mount at ``/mcp``.

Two layers of coverage:

* **HTTP mount** — ``/mcp`` is served by the FastMCP sub-app and does
  not shadow ``/healthz``. The mount pattern (FastMCP 3.2.4+,
  verified against 3.4.6) is::

      mcp_app = mcp.http_app(path="/")
      app = FastAPI(lifespan=mcp_app.lifespan)
      app.mount("/mcp", mcp_app)

  NOT ``FastMCP.mount(app, path)`` — that method does not exist on
  FastMCP.

* **In-process FastMCP client** — open a :class:`fastmcp.Client`
  against the registered :class:`FastMCP` instance and call
  ``list_tools()``. PR1 (002-mcp-tools) MUST return the 2 read-only
  tools (``list_projects`` and ``search_code``). The other 4
  land in PR2 / PR3.

The ``test_mcp_tools_list_e2e`` test is the cross-phase gate G4 from
``tasks.md`` — it MUST pass before PR1 is marked complete.
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


class TestMcpToolsListE2E:
    """``client.list_tools()`` returns the 2 PR1 read-only tools.

    002-mcp-tools cross-phase gate G4. Drives the FastMCP instance
    IN-PROCESS via the in-memory ``Client`` (FastMCP 3.4.6 docs) so
    no network round-trip happens — the test is fast and isolated.
    """

    async def test_list_tools_returns_list_projects_and_search_code(self) -> None:
        from fastmcp import Client

        from mcp_server.interfaces.mcp.server import mcp

        async with Client(mcp) as client:
            tools = await client.list_tools()

        names = {t.name for t in tools}
        assert "list_projects" in names, (
            f"expected 'list_projects' in tool list, got {names}"
        )
        assert "search_code" in names, (
            f"expected 'search_code' in tool list, got {names}"
        )

    async def test_list_projects_tool_call_returns_sanitized_dicts(self) -> None:
        """End-to-end smoke: call ``list_projects`` through the MCP client."""
        from fastmcp import Client

        from mcp_server.interfaces.mcp.server import mcp

        async with Client(mcp) as client:
            result = await client.call_tool("list_projects", {})

        # ``call_tool`` returns a ``CallToolResult``; the structured
        # payload lives in ``.data`` (fastmcp 3.4.6) or
        # ``.structured_content`` depending on the response shape.
        # Both are checked defensively.
        payload = _extract_payload(result)
        assert isinstance(payload, list)
        # The real manifest declares >= 1 project; PR1 just needs the
        # call to succeed.
        assert len(payload) >= 1
        first = payload[0]
        for key in ("id", "display_name", "description", "index_chunk_count"):
            assert key in first, f"missing {key} in {first}"

    async def test_search_code_tool_call_returns_sanitized_list(self) -> None:
        """End-to-end smoke: call ``search_code`` through the MCP client.

        With no preindexed data, the search returns ``[]`` — but the
        call MUST succeed (the use case is wired, the tool is
        registered, no exception leaks).
        """
        from fastmcp import Client

        from mcp_server.interfaces.mcp.server import mcp

        async with Client(mcp) as client:
            result = await client.call_tool(
                "search_code", {"query": "rate limiting"}
            )

        payload = _extract_payload(result)
        assert isinstance(payload, list)


def _extract_payload(call_result: object) -> object:
    """Return the tool's structured payload from a ``CallToolResult``.

    FastMCP 3.4.6 stores the tool's return value in multiple places
    depending on whether the tool returned a primitive, a dict, or a
    Pydantic model. We check the most common fields in order.
    """
    for attr in ("data", "structured_content"):
        val = getattr(call_result, attr, None)
        if val is not None:
            return val
    # Fallback: the ``content`` list carries a TextContent with JSON.
    content = getattr(call_result, "content", None)
    if content:
        import json

        first = content[0]
        text = getattr(first, "text", None)
        if text:
            try:
                return json.loads(text)
            except (TypeError, ValueError):
                return text
    return None
