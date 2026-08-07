"""Browser-friendly MCP UI.

The ``/mcp/`` endpoint is the JSON-RPC transport for MCP clients
(Claude Desktop, Cursor, MCP Inspector). It does NOT return HTML on
GET — a browser visiting ``/mcp/`` gets a 307 redirect. This module
adds a sibling route at ``/mcp-ui`` that:

1. Renders a static HTML page that describes the server (name,
   version, capabilities, protocol version).
2. Lists every tool the server exposes, fetched from the same
   composition root that backs ``/mcp/`` (so the UI can never drift
   from the canonical schema).
3. Provides one form per tool. Each form's fields are inferred
   from the tool's JSON Schema (``inputSchema.properties``); a JSON
   textarea fallback handles schemas with no extractable fields.
4. Submits the form via ``fetch()`` to the real ``/mcp/`` JSON-RPC
   endpoint with the proper ``Accept: application/json,
   text/event-stream`` header and the two-step ``initialize`` →
   ``tools/call`` handshake.
5. Renders the JSON-RPC response (success or error) inline.

The page is a single static template; the only Python is the
``GET /mcp-ui`` handler that injects the server identity and the
``tools/list`` snapshot. Everything else is the browser doing the
work — no WebSocket, no SSE, no HTMX dependency for the MCP view.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from mcp_server.interfaces.http.web.templates import templates
from mcp_server.interfaces.mcp.server import mcp as _mcp_instance

__all__ = ["build_mcp_browser_router"]


# Field types the browser form can render as a native input. Anything
# else falls back to a JSON textarea so the tool is always invokable
# even when the schema is exotic.
_NATIVE_INPUT_TYPES = {
    "string": "text",
    "integer": "number",
    "number": "number",
    "boolean": "checkbox",
}


def _fields_from_schema(input_schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten a JSON Schema's ``properties`` into a form-friendly list.

    Each field exposes: ``name``, ``label``, ``type``, ``required``,
    ``default``, ``description``, and ``enum``. Native input types
    (string / integer / number / boolean) get a typed input; anything
    else (object, array, oneOf, etc.) is rendered as a JSON textarea
    so the user can still pass a value manually.
    """
    properties: dict[str, Any] = input_schema.get("properties", {}) or {}
    required_list: list[str] = list(input_schema.get("required", []) or [])

    out: list[dict[str, Any]] = []
    for name, spec in properties.items():
        json_type = spec.get("type", "string")
        if isinstance(json_type, list):
            # Nullable / multi-type → use the first non-null one.
            json_type = next((t for t in json_type if t != "null"), "string")
        field: dict[str, Any] = {
            "name": name,
            "label": name.replace("_", " ").title(),
            "type": _NATIVE_INPUT_TYPES.get(json_type, "json"),
            "required": name in required_list,
            "default": spec.get("default"),
            "description": spec.get("description", ""),
            "enum": spec.get("enum"),
        }
        out.append(field)
    return out

def _serialize_tools(tools: Iterable[Any]) -> list[dict[str, Any]]:
    """Normalize the FastMCP ``list_tools()`` payload to a plain list
    of dicts the Jinja template can render without FastMCP types.
    """
    serialized: list[dict[str, Any]] = []
    for tool in tools:
        # FastMCP 3.4.6 returns FunctionTool (not a dict). The fields
        # we need are all attributes: ``name``, ``description``,
        # ``inputSchema`` (a pydantic model — use ``model_dump``).
        name = getattr(tool, "name", "") or ""
        description = getattr(tool, "description", "") or ""
        input_schema = getattr(tool, "inputSchema", None)
        if input_schema is None:
            input_schema = {}
        elif hasattr(input_schema, "model_dump"):
            input_schema = input_schema.model_dump()
        elif hasattr(input_schema, "dict"):
            input_schema = input_schema.dict()
        elif not isinstance(input_schema, dict):
            input_schema = {}

        serialized.append(
            {
                "name": name,
                "description": description,
                "input_schema_json": json.dumps(input_schema, indent=2),
                "fields": _fields_from_schema(input_schema),
                "raw_schema": input_schema,
            }
        )
    return serialized


def build_mcp_browser_router() -> APIRouter:
    """Build the ``/mcp-ui`` router. The page lives next to the
    playground routes; the actual JSON-RPC transport remains at
    ``/mcp/`` for external clients.
    """
    router = APIRouter()

    @router.get("/mcp-ui", response_class=HTMLResponse, name="mcp_browser")
    async def mcp_browser(request: Request) -> HTMLResponse:
        # Import the FastMCP instance directly (the canonical source
        # for tool list + server identity). Importing at request-time
        # avoids the import-order hazard with the tool-registration
        # side-effect import in ``interfaces.mcp.server``.
        from mcp_server.interfaces.mcp.server import mcp
        server_info: dict[str, Any] = {
            "name": "mcp-server-playground",
            "version": "0.1.0",
            "protocol": "2024-11-05",
            "tools_count": 0,
            "tools": [],
            "mcp_url": "/mcp/",
        }
        try:
            tools = await mcp.list_tools()
            server_info["tools"] = _serialize_tools(tools)
            server_info["tools_count"] = len(server_info["tools"])
        except Exception as exc:  # noqa: BLE001 — surface to UI
            server_info["error"] = f"{type(exc).__name__}: {exc}"
        # The template reads ``server.*`` so wrap the dict under that name.
        return templates.TemplateResponse(
            request, "mcp_browser.html", {"server": server_info}
        )

    return router


def _resolve_mcp_browser_template() -> Path | None:
    """Return the absolute path to ``mcp_browser.html`` if it exists.

    Used by tests that want to render the template directly without
    spinning up the FastAPI app.
    """
    candidates = [
        Path(__file__).parent.parent.parent.parent.parent.parent
        / "playground"
        / "templates"
        / "mcp_browser.html",
        Path.cwd() / "playground" / "templates" / "mcp_browser.html",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None
