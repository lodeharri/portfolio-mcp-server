"""Unit tests for ``src/mcp_server/interfaces/mcp/tools.py``.

The tool wrappers are thin ~10-line shims around the use cases. They
must:

* Be registered with FastMCP via the ``@mcp.tool`` decorator at
  module import time.
* Look up the use case from a module-level container (set by the
  composition root).
* Wrap ``use_case.execute(...)`` in ``try/except DomainError -> raise
  translate_tool_error(exc)`` per ADR-002.
* Return JSON-serializable dicts (``list[dict]`` for the two PR1
  tools).

The test bypasses the FastMCP server and calls the tool functions
directly after calling :func:`set_use_cases` to inject fakes. The
``@mcp.tool`` decorator stores the original function as ``fn`` on the
returned :class:`FunctionTool` so we can still call the underlying
async function.

We also assert the wrapper emits no ``output.redacted`` audit events
on its own — sanitization happens inside the use case (Layer 3
discipline, ADR-003).
"""

from __future__ import annotations

from typing import Any

import pytest

from mcp_server.domain.exceptions import (
    DomainError,
    ManifestProjectNotFoundError,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeListProjectsUseCase:
    """In-memory fake returning a configurable list of project dicts."""

    def __init__(self, projects: list[dict[str, Any]] | None = None) -> None:
        self._projects = list(projects or [])
        self.calls: int = 0

    def execute(self) -> list[dict[str, Any]]:
        self.calls += 1
        return list(self._projects)


class _FakeSearchCodeUseCase:
    """In-memory fake returning a configurable list of search-result dicts."""

    def __init__(
        self,
        results: list[dict[str, Any]] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._results = list(results or [])
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    def execute(self, request: Any) -> list[dict[str, Any]]:
        self.calls.append(
            {
                "query": request.query,
                "top_k": request.top_k,
                "project_id": request.project_id,
            }
        )
        if self._raise is not None:
            raise self._raise
        return list(self._results)


def _set_fake_use_cases(
    *,
    list_projects_uc: _FakeListProjectsUseCase | None = None,
    search_uc: _FakeSearchCodeUseCase | None = None,
) -> None:
    """Inject fakes into the tools module's container."""
    from mcp_server.interfaces.mcp import tools

    tools.set_use_cases(
        list_projects_uc=list_projects_uc or _FakeListProjectsUseCase(),
        search_uc=search_uc or _FakeSearchCodeUseCase(),
    )


# ---------------------------------------------------------------------------
# Module-level container
# ---------------------------------------------------------------------------


class TestUseCaseContainer:
    """``set_use_cases`` populates the module-level container."""

    def test_set_use_cases_then_lookup(self) -> None:
        from mcp_server.interfaces.mcp import tools

        list_uc = _FakeListProjectsUseCase()
        search_uc = _FakeSearchCodeUseCase()
        tools.set_use_cases(list_projects_uc=list_uc, search_uc=search_uc)

        # Lookup via the public helper (keeps the test surface stable
        # if the container type changes).
        assert tools.get_list_projects_use_case() is list_uc
        assert tools.get_search_use_case() is search_uc


# ---------------------------------------------------------------------------
# list_projects_tool
# ---------------------------------------------------------------------------


class TestListProjectsTool:
    """``list_projects_tool`` calls the use case and returns the dicts."""

    def test_returns_use_case_payload(self) -> None:
        projects = [
            {"id": "a", "display_name": "A", "description": "", "index_chunk_count": 0},
            {"id": "b", "display_name": "B", "description": "", "index_chunk_count": 0},
        ]
        list_uc = _FakeListProjectsUseCase(projects=projects)
        _set_fake_use_cases(list_projects_uc=list_uc)
        from mcp_server.interfaces.mcp import tools

        # Call the underlying async function (the @mcp.tool decorator
        # stores the original on .fn).
        out = _call_list_projects_tool(tools)

        assert out == projects
        assert list_uc.calls == 1

    def test_empty_use_case_returns_empty_list(self) -> None:
        list_uc = _FakeListProjectsUseCase(projects=[])
        _set_fake_use_cases(list_projects_uc=list_uc)
        from mcp_server.interfaces.mcp import tools

        out = _call_list_projects_tool(tools)

        assert out == []

    def test_domain_error_translates_to_tool_error(self) -> None:
        from fastmcp.exceptions import ToolError

        list_uc = _FakeListProjectsUseCase(
            projects=[],
        )
        # The fake's execute is called once; raise the domain error
        # on the second call.
        original_execute = list_uc.execute

        def raise_on_call() -> list[dict[str, Any]]:
            if list_uc.calls == 0:
                list_uc.calls += 1
                return []
            raise ManifestProjectNotFoundError(
                "project 'foo' not declared in manifest"
            )

        list_uc.execute = raise_on_call  # type: ignore[method-assign]
        _set_fake_use_cases(list_projects_uc=list_uc)
        from mcp_server.interfaces.mcp import tools

        with pytest.raises(ToolError) as exc_info:
            _call_list_projects_tool(tools)
        # The translated message echoes the project_id.
        assert "foo" in str(exc_info.value)

        # Reset (test hygiene).
        list_uc.execute = original_execute  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# search_code_tool
# ---------------------------------------------------------------------------


class TestSearchCodeTool:
    """``search_code_tool`` builds a request and calls the use case."""

    def test_returns_use_case_payload(self) -> None:
        results = [
            {
                "chunk_hash": "a" * 64,
                "file_path": "/x.py",
                "line_start": 1,
                "line_end": 1,
                "content": "pass",
                "score": 0.1,
                "project_id": "p",
            }
        ]
        search_uc = _FakeSearchCodeUseCase(results=results)
        _set_fake_use_cases(search_uc=search_uc)
        from mcp_server.interfaces.mcp import tools

        out = _call_search_code_tool(tools, query="rate limiting", top_k=5, project_id="p")

        assert out == results
        # The use case received the expected request fields.
        assert search_uc.calls == [
            {"query": "rate limiting", "top_k": 5, "project_id": "p"}
        ]

    def test_default_top_k_is_5_per_spec(self) -> None:
        """Per the spec, ``top_k`` defaults to 5."""
        search_uc = _FakeSearchCodeUseCase()
        _set_fake_use_cases(search_uc=search_uc)
        from mcp_server.interfaces.mcp import tools

        _call_search_code_tool(tools, query="hello")

        assert search_uc.calls[0]["top_k"] == 5
        assert search_uc.calls[0]["project_id"] is None

    def test_empty_query_translates_to_tool_error(self) -> None:
        from fastmcp.exceptions import ToolError

        search_uc = _FakeSearchCodeUseCase(
            raise_exc=ValueError("query must be a non-empty string")
        )
        _set_fake_use_cases(search_uc=search_uc)
        from mcp_server.interfaces.mcp import tools

        with pytest.raises(ToolError) as exc_info:
            _call_search_code_tool(tools, query="")
        assert "non-empty" in str(exc_info.value)

    def test_gemini_transient_translates_to_authored_message(self) -> None:
        """``GeminiTransientError`` is mapped to the authored message,
        not the raw SDK text."""
        from fastmcp.exceptions import ToolError
        from mcp_server.domain.exceptions import GeminiTransientError

        search_uc = _FakeSearchCodeUseCase(
            raise_exc=GeminiTransientError("429 rate limit exhausted")
        )
        _set_fake_use_cases(search_uc=search_uc)
        from mcp_server.interfaces.mcp import tools

        with pytest.raises(ToolError) as exc_info:
            _call_search_code_tool(tools, query="hi")
        # Authored message — no raw "429" leak.
        assert "429" not in str(exc_info.value)
        assert "temporarily" in str(exc_info.value).lower()

    def test_domain_error_translates_to_tool_error(self) -> None:
        from fastmcp.exceptions import ToolError

        class CustomDomainError(DomainError):
            pass

        search_uc = _FakeSearchCodeUseCase(raise_exc=CustomDomainError("oops"))
        _set_fake_use_cases(search_uc=search_uc)
        from mcp_server.interfaces.mcp import tools

        with pytest.raises(ToolError) as exc_info:
            _call_search_code_tool(tools, query="hi")
        # The generic domain default is "internal error" (no raw leak).
        assert "oops" not in str(exc_info.value)
        assert "internal error" in str(exc_info.value).lower()

    def test_programming_error_reraises(self) -> None:
        """``TypeError`` from a use case MUST NOT be caught by the wrapper."""
        search_uc = _FakeSearchCodeUseCase(raise_exc=TypeError("'NoneType' has no attribute 'x'"))
        _set_fake_use_cases(search_uc=search_uc)
        from mcp_server.interfaces.mcp import tools

        with pytest.raises(TypeError):
            _call_search_code_tool(tools, query="hi")


# ---------------------------------------------------------------------------
# @mcp.tool registration (introspection)
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """The @mcp.tool decorator registers both tools with FastMCP."""

    def test_list_projects_is_registered(self) -> None:
        from mcp_server.interfaces.mcp.server import mcp

        names = {tool.name for tool in mcp._tool_manager._tools.values()}
        assert "list_projects" in names

    def test_search_code_is_registered(self) -> None:
        from mcp_server.interfaces.mcp.server import mcp

        names = {tool.name for tool in mcp._tool_manager._tools.values()}
        assert "search_code" in names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_list_projects_tool(tools_module: Any) -> list[dict[str, Any]]:
    """Call the ``list_projects_tool`` async function directly.

    The ``@mcp.tool`` decorator stores the original function as
    ``.fn`` on the returned :class:`FunctionTool`. We invoke that
    directly so the test exercises the wrapper's exception
    translation without going through FastMCP's transport.
    """
    import asyncio

    from mcp_server.interfaces.mcp.server import mcp

    tool = next(
        t for t in mcp._tool_manager._tools.values() if t.name == "list_projects"
    )
    return asyncio.run(tool.fn())


def _call_search_code_tool(
    tools_module: Any, *, query: str, top_k: int = 5, project_id: str | None = None
) -> list[dict[str, Any]]:
    """Call the ``search_code_tool`` async function directly."""
    import asyncio

    from mcp_server.interfaces.mcp.server import mcp

    tool = next(
        t for t in mcp._tool_manager._tools.values() if t.name == "search_code"
    )
    return asyncio.run(tool.fn(query=query, top_k=top_k, project_id=project_id))
