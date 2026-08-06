"""End-to-end smoke test for the ``ask_portfolio`` MCP tool (PR3).

Boots the FastMCP ``Client`` in-process (per FastMCP 3.4.6 docs) and
verifies the new meta-tool is reachable via ``tools/call``.

The composition root runs in ``--mock-gemini`` mode (no API key in
the test env) so the Pydantic AI agent is built with a
:class:`pydantic_ai.models.function.FunctionModel` that emits a
deterministic ``"[mock answer to: hi]"`` reply. The test asserts on
that contract — not on real LLM output.

Two layers of coverage:

1. **Tool list** — the FastMCP server's tool list now contains all 6
   tools (PR1 × 2 + PR2 × 3 + PR3 × 1). This is the cross-phase gate
   G4 from ``tasks.md``.
2. **Tool call** — ``ask_portfolio`` returns a dict with ``answer``,
   ``tools_called``, and ``conversation_id`` keys (the response shape
   defined in the spec).
"""

from __future__ import annotations

import asyncio


def _six_tool_names() -> set[str]:
    """Return the names of all 6 MCP tools registered on the FastMCP instance."""

    async def names() -> set[str]:
        from mcp_server.interfaces.mcp.server import mcp

        return {tool.name for tool in await mcp.list_tools()}

    return asyncio.run(names())


def test_all_six_tools_are_registered() -> None:
    """Cross-phase gate G4: the FastMCP instance registers all 6 tools.

    * PR1: ``list_projects``, ``search_code``
    * PR2: ``explain_architecture``, ``summarize_readme``, ``get_architecture_diagram``
    * PR3: ``ask_portfolio``
    """
    names = _six_tool_names()
    expected = {
        "list_projects",
        "search_code",
        "explain_architecture",
        "summarize_readme",
        "get_architecture_diagram",
        "ask_portfolio",
    }
    assert expected <= names, f"missing tools: {expected - names}"


def test_ask_portfolio_call_returns_mock_answer() -> None:
    """End-to-end smoke: ``ask_portfolio`` returns the deterministic mock answer."""
    from fastmcp import Client

    from mcp_server.interfaces.mcp.server import mcp

    async def call() -> object:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "ask_portfolio", {"question": "list projects"}
            )
        return result

    result = asyncio.run(call())

    # The mock FunctionModel emits ``[mock answer to: hi]`` (the literal
    # string, not interpolated from the question — the agent sees the
    # question but the mock doesn't parse it). This is the documented
    # contract for ``--mock-gemini`` mode (per design.md + spec).
    payload = _extract_payload(result)
    assert isinstance(payload, dict)
    assert payload["answer"] == "[mock answer to: hi]"
    assert payload["tools_called"] == []
    assert payload["conversation_id"] is None


def test_ask_portfolio_call_with_conversation_id_echoes_it() -> None:
    """The ``conversation_id`` argument is echoed back in the response."""
    from fastmcp import Client

    from mcp_server.interfaces.mcp.server import mcp

    async def call() -> object:
        async with Client(mcp) as client:
            result = await client.call_tool(
                "ask_portfolio",
                {"question": "any", "conversation_id": "conv-42"},
            )
        return result

    result = asyncio.run(call())
    payload = _extract_payload(result)
    assert isinstance(payload, dict)
    assert payload["conversation_id"] == "conv-42"


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
