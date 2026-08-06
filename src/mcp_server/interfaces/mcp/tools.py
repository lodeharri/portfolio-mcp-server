"""``@mcp.tool`` registrations for the 6 MCP tools — PR1 (read-only).

This module owns the FastMCP ``@mcp.tool`` decorators for the 2
read-only tools in 002-mcp-tools PR1:

* ``list_projects`` — manifest read.
* ``search_code`` — embed + vector search.

PR2 will append ``explain_architecture``, ``summarize_readme``, and
``get_architecture_diagram``. PR3 will append ``ask_portfolio``.

Why a module-level container?
-----------------------------

The tool functions must reference the use cases built by the
composition root, but ``composition.py`` and ``interfaces/mcp/server.py``
sit on different branches of the import graph (the composition wires
adapters to use cases; the server only declares the FastMCP instance).
A direct import would create a cycle.

Solution: each tool function looks up the use case from a
module-level container that the composition root populates. The
container is a typed dict; ``set_use_cases()`` is the only public
mutation entry point. The composition root calls ``set_use_cases``
once at startup, after all use cases are built.

Why a ``try/except`` instead of letting exceptions bubble?
----------------------------------------------------------

The use cases raise ``ValueError`` / ``DomainError`` subclasses
per their spec contracts. The wrapper translates these to
``ToolError`` (FastMCP's signal for "this tool failed") via
``translate_tool_error`` (ADR-002). Programming errors
(``TypeError`` / ``AttributeError``) re-raise so the maintainer
notices the bug instead of getting a silently mapped generic error.

Why no LLM / no FastAPI?
------------------------

The tool wrappers are pure shims. All sanitization (Layer 3) and
audit emission (Layer 5) happens INSIDE the use cases — the
wrappers just call ``use_case.execute(...)`` and forward the
result. This is the ADR-003 "sanitize at the source" discipline.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mcp_server.application.use_cases.explain_architecture import (
    ExplainArchitectureRequest,
    ExplainArchitectureUseCase,
)
from mcp_server.application.use_cases.get_architecture_diagram import (
    GetArchitectureDiagramRequest,
    GetArchitectureDiagramUseCase,
)
from mcp_server.application.use_cases.list_projects import ListProjectsUseCase
from mcp_server.application.use_cases.search_code import SearchCodeRequest, SearchCodeUseCase
from mcp_server.application.use_cases.summarize_readme import (
    SummarizeReadmeRequest,
    SummarizeReadmeUseCase,
)
from mcp_server.domain.exceptions import DomainError
from mcp_server.interfaces.mcp.server import mcp
from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

__all__ = [
    "explain_architecture_tool",
    "get_architecture_diagram_tool",
    "get_list_projects_use_case",
    "get_search_use_case",
    "list_projects_tool",
    "search_code_tool",
    "set_use_cases",
    "summarize_readme_tool",
]


# ---------------------------------------------------------------------------
# Module-level use case container
# ---------------------------------------------------------------------------


# Typed use case references. ``set_use_cases`` is the only public
# mutation entry point; tools read these at call time (NOT at
# decorator time) so the composition root can populate them after
# importing this module.
_list_projects_use_case: ListProjectsUseCase | None = None
_search_use_case: SearchCodeUseCase | None = None
_explain_architecture_use_case: ExplainArchitectureUseCase | None = None
_summarize_readme_use_case: SummarizeReadmeUseCase | None = None
_get_architecture_diagram_use_case: GetArchitectureDiagramUseCase | None = None


def set_use_cases(
    *,
    list_projects_uc: ListProjectsUseCase,
    search_uc: SearchCodeUseCase,
    explain_architecture_uc: ExplainArchitectureUseCase | None = None,
    summarize_readme_uc: SummarizeReadmeUseCase | None = None,
    get_architecture_diagram_uc: GetArchitectureDiagramUseCase | None = None,
) -> None:
    """Populate the module-level use case container.

    Called by the composition root (``create_composition``) after all
    use cases are built. Idempotent — a second call replaces the
    previous references (useful for tests that swap fakes in/out).
    """
    global _list_projects_use_case, _search_use_case
    global _explain_architecture_use_case, _summarize_readme_use_case
    global _get_architecture_diagram_use_case
    _list_projects_use_case = list_projects_uc
    _search_use_case = search_uc
    _explain_architecture_use_case = explain_architecture_uc
    _summarize_readme_use_case = summarize_readme_uc
    _get_architecture_diagram_use_case = get_architecture_diagram_uc


def get_list_projects_use_case() -> ListProjectsUseCase:
    """Return the wired ``ListProjectsUseCase``; raise ``ToolError`` if missing."""
    if _list_projects_use_case is None:
        # Should never happen in production — composition wires it at
        # startup. Programmer error if it does.
        raise RuntimeError(
            "ListProjectsUseCase not wired — composition root must call "
            "set_use_cases() before serving requests"
        )
    return _list_projects_use_case


def get_search_use_case() -> SearchCodeUseCase:
    """Return the wired ``SearchCodeUseCase``; raise ``ToolError`` if missing."""
    if _search_use_case is None:
        raise RuntimeError(
            "SearchCodeUseCase not wired — composition root must call "
            "set_use_cases() before serving requests"
        )
    return _search_use_case


# ---------------------------------------------------------------------------
# list_projects — read-only manifest read
# ---------------------------------------------------------------------------


@mcp.tool(
    name="list_projects",
    description=(
        "List the portfolio projects declared in projects.manifest.yaml. "
        "Returns one entry per project with id, display_name, description, "
        "and index_chunk_count. Output is sanitized (Layer 3) — token-shaped "
        "substrings in description are replaced with [REDACTED] before the "
        "response leaves the server."
    ),
)
async def list_projects_tool() -> list[dict[str, Any]]:
    """Return the sanitized list of declared portfolio projects.

    Returns:
        ``list[dict]`` — one dict per declared project with keys
        ``id``, ``display_name``, ``description``, ``index_chunk_count``.

    Raises:
        ToolError: ``-32603`` internal error if the use case was not
            wired by the composition root (programmer error).
    """
    use_case = get_list_projects_use_case()
    try:
        return use_case.execute()
    except DomainError as exc:
        raise translate_tool_error(exc) from exc


# ---------------------------------------------------------------------------
# search_code — semantic search over indexed chunks
# ---------------------------------------------------------------------------


@mcp.tool(
    name="search_code",
    description=(
        "Semantic search over the indexed code chunks built by the "
        "preindex pipeline. Embeds the query, runs vector similarity "
        "search against the SQLite-vec index, returns the top-k matches "
        "with chunk_hash, file_path, line_start/line_end, content, score, "
        "and project_id. Output is sanitized (Layer 3) — token-shaped "
        "substrings in chunk content are replaced with [REDACTED]."
    ),
)
async def search_code_tool(
    query: str,
    top_k: int = 5,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return sanitized top-k search results for ``query``.

    Args:
        query: Natural-language query. MUST be non-empty.
        top_k: Maximum results to return (capped at 50 by the use case).
        project_id: Optional scope filter — restrict results to one project.

    Returns:
        ``list[dict]`` — one dict per result with keys ``chunk_hash``,
        ``file_path``, ``line_start``, ``line_end``, ``content`` (sanitized),
        ``score``, ``project_id``.

    Raises:
        ToolError: ``-32602`` invalid params on empty query or
            ``top_k > 50``; ``-32603`` internal error on embedding
            failures (Gemini 429 / 5xx) or vector-store dim mismatch.
    """
    use_case = get_search_use_case()
    try:
        return use_case.execute(
            SearchCodeRequest(query=query, top_k=top_k, project_id=project_id)
        )
    except DomainError as exc:
        raise translate_tool_error(exc) from exc
    except ValueError as exc:
        raise translate_tool_error(exc) from exc


def _require_use_case(use_case: Any, name: str) -> Any:
    if use_case is None:
        raise RuntimeError(f"{name} not wired — composition root must call set_use_cases()")
    return use_case


def _as_payload(result: Any) -> dict[str, Any]:
    return asdict(result) if hasattr(result, "__dataclass_fields__") else dict(result)


@mcp.tool(
    name="explain_architecture",
    description="Summarize a project's architecture from its ADRs.",
)
async def explain_architecture_tool(project_id: str, max_tokens: int = 500) -> dict[str, Any]:
    try:
        result = _require_use_case(
            _explain_architecture_use_case, "ExplainArchitectureUseCase"
        ).execute(
            ExplainArchitectureRequest(project_id=project_id, max_tokens=max_tokens)
        )
        return _as_payload(result)
    except (DomainError, ValueError, FileNotFoundError) as exc:
        raise translate_tool_error(exc) from exc


@mcp.tool(
    name="summarize_readme",
    description="Summarize a project's README in recruiter-friendly prose.",
)
async def summarize_readme_tool(project_id: str, max_tokens: int = 300) -> dict[str, Any]:
    try:
        result = _require_use_case(_summarize_readme_use_case, "SummarizeReadmeUseCase").execute(
            SummarizeReadmeRequest(project_id=project_id, max_tokens=max_tokens)
        )
        return _as_payload(result)
    except (DomainError, ValueError, FileNotFoundError) as exc:
        raise translate_tool_error(exc) from exc


@mcp.tool(
    name="get_architecture_diagram",
    description="Return a project's architecture diagram as base64-encoded SVG.",
)
async def get_architecture_diagram_tool(project_id: str) -> dict[str, Any]:
    try:
        result = _require_use_case(
            _get_architecture_diagram_use_case, "GetArchitectureDiagramUseCase"
        ).execute(GetArchitectureDiagramRequest(project_id=project_id))
        return _as_payload(result)
    except (DomainError, ValueError, FileNotFoundError) as exc:
        raise translate_tool_error(exc) from exc
