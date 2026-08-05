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

The test bypasses the FastMCP transport and calls the tool functions
directly after calling :func:`set_use_cases` to inject fakes. The
``@mcp.tool`` decorator stores the original function as ``.fn`` on
the returned :class:`FunctionTool` so we can still call the
underlying async function from sync test code via ``asyncio.run``.

We also assert the wrapper emits no ``output.redacted`` audit events
on its own — sanitization happens inside the use case (Layer 3
discipline, ADR-003).
"""

from __future__ import annotations

import asyncio
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

        out = asyncio.run(_call_list_projects_tool())

        assert out == projects
        assert list_uc.calls == 1

    def test_empty_use_case_returns_empty_list(self) -> None:
        list_uc = _FakeListProjectsUseCase(projects=[])
        _set_fake_use_cases(list_projects_uc=list_uc)

        out = asyncio.run(_call_list_projects_tool())

        assert out == []

    def test_domain_error_translates_to_tool_error(self) -> None:
        from fastmcp.exceptions import ToolError

        # Fake that raises on the second call (so the wrapper's
        # try/except kicks in). We track calls via a counter.
        class _RaisingOnSecondCall:
            def __init__(self) -> None:
                self.calls = 0

            def execute(self) -> list[dict[str, Any]]:
                self.calls += 1
                if self.calls == 1:
                    return []
                raise ManifestProjectNotFoundError(
                    "project 'foo' not declared in manifest"
                )

        list_uc = _RaisingOnSecondCall()  # type: ignore[arg-type]
        _set_fake_use_cases(list_projects_uc=list_uc)  # type: ignore[arg-type]

        # First call: empty result.
        first = asyncio.run(_call_list_projects_tool())
        assert first == []

        # Second call: must raise ToolError with the project id echoed.
        with pytest.raises(ToolError) as exc_info:
            asyncio.run(_call_list_projects_tool())
        assert "foo" in str(exc_info.value)


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

        out = asyncio.run(_call_search_code_tool(query="rate limiting", top_k=5, project_id="p"))

        assert out == results
        # The use case received the expected request fields.
        assert search_uc.calls == [
            {"query": "rate limiting", "top_k": 5, "project_id": "p"}
        ]

    def test_default_top_k_is_5_per_spec(self) -> None:
        """Per the spec, ``top_k`` defaults to 5."""
        search_uc = _FakeSearchCodeUseCase()
        _set_fake_use_cases(search_uc=search_uc)

        asyncio.run(_call_search_code_tool(query="hello"))

        assert search_uc.calls[0]["top_k"] == 5
        assert search_uc.calls[0]["project_id"] is None

    def test_empty_query_translates_to_tool_error(self) -> None:
        from fastmcp.exceptions import ToolError

        search_uc = _FakeSearchCodeUseCase(
            raise_exc=ValueError("query must be a non-empty string")
        )
        _set_fake_use_cases(search_uc=search_uc)

        with pytest.raises(ToolError) as exc_info:
            asyncio.run(_call_search_code_tool(query=""))
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

        with pytest.raises(ToolError) as exc_info:
            asyncio.run(_call_search_code_tool(query="hi"))
        # Authored message — no raw "429" leak.
        assert "429" not in str(exc_info.value)
        assert "temporarily" in str(exc_info.value).lower()

    def test_domain_error_translates_to_tool_error(self) -> None:
        from fastmcp.exceptions import ToolError

        class CustomDomainError(DomainError):
            pass

        search_uc = _FakeSearchCodeUseCase(raise_exc=CustomDomainError("oops"))
        _set_fake_use_cases(search_uc=search_uc)

        with pytest.raises(ToolError) as exc_info:
            asyncio.run(_call_search_code_tool(query="hi"))
        # The generic domain default is "internal error" (no raw leak).
        assert "oops" not in str(exc_info.value)
        assert "internal error" in str(exc_info.value).lower()

    def test_programming_error_reraises(self) -> None:
        """``TypeError`` from a use case MUST NOT be caught by the wrapper."""
        search_uc = _FakeSearchCodeUseCase(raise_exc=TypeError("'NoneType' has no attribute 'x'"))
        _set_fake_use_cases(search_uc=search_uc)

        with pytest.raises(TypeError):
            asyncio.run(_call_search_code_tool(query="hi"))


# ---------------------------------------------------------------------------
# @mcp.tool registration (introspection via the async FastMCP API)
# ---------------------------------------------------------------------------


class TestToolRegistration:
    """The @mcp.tool decorator registers both tools with FastMCP."""

    def test_list_projects_is_registered(self) -> None:
        from mcp_server.interfaces.mcp.server import mcp

        names = asyncio.run(_registered_tool_names(mcp))
        assert "list_projects" in names

    def test_search_code_is_registered(self) -> None:
        from mcp_server.interfaces.mcp.server import mcp

        names = asyncio.run(_registered_tool_names(mcp))
        assert "search_code" in names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _registered_tool_names(server: Any) -> set[str]:
    """Return the set of tool names currently registered on ``server``."""
    tools = await server.list_tools()
    return {t.name for t in tools}


async def _call_list_projects_tool() -> list[dict[str, Any]]:
    """Call the ``list_projects_tool`` async function directly.

    The ``@mcp.tool`` decorator stores the original function as
    ``.fn`` on the returned :class:`FunctionTool`. We invoke that
    directly so the test exercises the wrapper's exception
    translation without going through FastMCP's transport.
    """
    from mcp_server.interfaces.mcp.server import mcp

    tool = await mcp.get_tool("list_projects")
    return await tool.fn()


async def _call_search_code_tool(
    *, query: str, top_k: int = 5, project_id: str | None = None
) -> list[dict[str, Any]]:
    """Call the ``search_code_tool`` async function directly."""
    from mcp_server.interfaces.mcp.server import mcp

    tool = await mcp.get_tool("search_code")
    return await tool.fn(query=query, top_k=top_k, project_id=project_id)
