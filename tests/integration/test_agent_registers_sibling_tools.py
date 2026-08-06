"""Integration tests for LangChain agent tool composition."""

from __future__ import annotations

from mcp_server.composition import create_composition
from mcp_server.config import AppConfig

EXPECTED_TOOL_NAMES = {
    "list_projects_tool",
    "search_code_tool",
    "explain_architecture_tool",
    "summarize_readme_tool",
    "get_architecture_diagram_tool",
}


def _registered_tool_names() -> set[str]:
    composition = create_composition(AppConfig())
    use_case = composition.ask_portfolio_use_case
    assert use_case is not None
    return {
        getattr(tool, "name", None) or getattr(tool, "__name__", "")
        for tool in use_case.tools
    }


def test_agent_has_exactly_five_sibling_tools() -> None:
    assert _registered_tool_names() == EXPECTED_TOOL_NAMES


def test_agent_has_no_extra_tools() -> None:
    forbidden = {"Bash", "Shell", "HttpGet", "WebSearch", "ReadFile", "WriteFile"}

    assert not _registered_tool_names() & forbidden
