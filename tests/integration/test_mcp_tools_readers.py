"""FastMCP registration smoke tests for the PR2 reader tools."""

from __future__ import annotations

import asyncio

from mcp_server.interfaces.mcp.server import mcp


def test_all_five_pr1_and_pr2_tools_are_registered() -> None:
    async def names() -> set[str]:
        return {tool.name for tool in await mcp.list_tools()}

    assert asyncio.run(names()) >= {
        "list_projects",
        "search_code",
        "explain_architecture",
        "summarize_readme",
        "get_architecture_diagram",
    }
