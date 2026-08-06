"""Integration test for the Pydantic AI Agent tool composition.

After 002-mcp-tools PR3 lands, ``create_composition()`` MUST build a
Pydantic AI ``Agent`` that has the 5 sibling MCP tools registered as
function-calling tools (per ADR-001):

* ``list_projects_tool``
* ``search_code_tool``
* ``explain_architecture_tool``
* ``summarize_readme_tool``
* ``get_architecture_diagram_tool``

The agent MUST NOT have any other tools registered (no network, no
shell). This test guards against accidental drift (a 6th tool sneaks
in, or one of the 5 is dropped).

The test runs in-process against the real ``create_composition()``
output (mock-gemini mode is fine — the test inspects the agent's
toolsets, not its LLM behavior).
"""

from __future__ import annotations

from mcp_server.composition import create_composition
from mcp_server.config import AppConfig

EXPECTED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "list_projects_tool",
        "search_code_tool",
        "explain_architecture_tool",
        "summarize_readme_tool",
        "get_architecture_diagram_tool",
    }
)


def test_agent_has_exactly_five_sibling_tools() -> None:
    """The composition root MUST build an agent with exactly 5 tools."""
    comp = create_composition(AppConfig())

    assert comp.ask_portfolio_use_case is not None
    agent = comp.ask_portfolio_use_case.agent
    assert agent is not None

    # Pydantic AI exposes tool names via ``agent.toolsets`` — each
    # toolset has ``tools`` (a dict of ``Tool`` instances with
    # ``name`` / ``__name__`` attributes).
    discovered: set[str] = set()
    for toolset in agent.toolsets:
        # ``toolset.tools`` is the canonical dict of registered tools.
        tool_dict = getattr(toolset, "tools", None)
        if isinstance(tool_dict, dict):
            for tool in tool_dict.values():
                name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
                if name:
                    discovered.add(name)
    assert discovered == EXPECTED_TOOL_NAMES, (
        f"agent tools drifted: expected {EXPECTED_TOOL_NAMES}, got {discovered}"
    )


def test_agent_has_no_extra_tools() -> None:
    """Sanity: nothing else leaked in (no Bash, no HTTP, no shell)."""
    comp = create_composition(AppConfig())
    agent = comp.ask_portfolio_use_case.agent

    discovered: set[str] = set()
    for toolset in agent.toolsets:
        tool_dict = getattr(toolset, "tools", None)
        if isinstance(tool_dict, dict):
            for tool in tool_dict.values():
                name = getattr(tool, "name", None) or getattr(tool, "__name__", None)
                if name:
                    discovered.add(name)

    forbidden = {"Bash", "Shell", "HttpGet", "WebSearch", "ReadFile", "WriteFile"}
    leaked = discovered & forbidden
    assert not leaked, f"agent registered forbidden tools: {leaked}"
