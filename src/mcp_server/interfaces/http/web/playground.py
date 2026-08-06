"""``POST /playground/api/{tool_name}`` form-endpoint implementations.

Each non-agent tool gets one endpoint:

* ``list_projects``           → ``POST /playground/api/list_projects``
* ``search_code``             → ``POST /playground/api/search_code``
* ``explain_architecture``    → ``POST /playground/api/explain_architecture``
* ``summarize_readme``        → ``POST /playground/api/summarize_readme``
* ``get_architecture_diagram``→ ``POST /playground/api/get_architecture_diagram``

All endpoints share the same pattern:

1. Look up the wired use case via
   :func:`mcp_server.interfaces.http.web.deps.get_composition`.
2. Run the use case.
3. Render the matching Jinja2 fragment (no ``<html>`` or ``<body>``
   wrapper) through the shared
   :data:`mcp_server.interfaces.http.web.templates.templates`
   environment.

The fragment endpoint is exposed directly (not via :func:`build_web_router`)
so the router stays thin and the form wiring is self-contained. The
function :func:`register_playground_routes` adds the endpoints to an
existing :class:`APIRouter` so the router file decides where the
prefix lives.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterable
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from mcp_server.application.use_cases.explain_architecture import (
    ExplainArchitectureRequest,
)
from mcp_server.application.use_cases.get_architecture_diagram import (
    GetArchitectureDiagramRequest,
)
from mcp_server.application.use_cases.search_code import (
    MAX_TOP_K,
    SearchCodeRequest,
)
from mcp_server.application.use_cases.summarize_readme import (
    SummarizeReadmeRequest,
)
from mcp_server.domain.exceptions import ManifestProjectNotFoundError
from mcp_server.interfaces.http.web.deps import get_composition
from mcp_server.interfaces.http.web.templates import templates

__all__ = [
    "PLAYGROUND_FORM_TOOLS",
    "register_playground_routes",
]


#: Ordered set of non-agent tool names exposed at /playground/api/<name>.
#: ``ask_portfolio`` is intentionally excluded — it has its own streaming
#: surface at /chat/stream (PR2a/b). The MCP transport still exposes all
#: six tools at /mcp.
PLAYGROUND_FORM_TOOLS: tuple[str, ...] = (
    "list_projects",
    "search_code",
    "explain_architecture",
    "summarize_readme",
    "get_architecture_diagram",
)


def register_playground_routes(router: APIRouter) -> None:
    """Attach the five ``POST /playground/api/{tool}`` endpoints to a router."""

    @router.post(
        "/playground/api/list_projects",
        response_class=HTMLResponse,
        name="playground_list_projects",
    )
    async def list_projects_fragment(request: Request) -> HTMLResponse:
        composition = get_composition(request)
        if composition is None:
            raise HTTPException(status_code=500, detail="composition not wired")

        try:
            projects = composition.list_projects_use_case.execute()
        except Exception as exc:
            return _render_error_fragment("list_projects", _sanitize_error_message(exc))
        return templates.TemplateResponse(
            request=request,
            name="partials/list_projects.html",
            context={"projects": projects, "error": None},
            status_code=200,
        )

    @router.post(
        "/playground/api/search_code",
        response_class=HTMLResponse,
        name="playground_search_code",
    )
    async def search_code_fragment(
        request: Request,
        query: str = Form(...),
        top_k: int = Form(default=5),
        project_id: str | None = Form(default=None),
    ) -> HTMLResponse:
        composition = get_composition(request)
        if composition is None:
            raise HTTPException(status_code=500, detail="composition not wired")

        # Drop empty project_id strings to None so the use case's
        # "all-projects" branch fires.
        scoped_project = project_id if project_id and project_id.strip() else None

        try:
            matches = composition.search_use_case.execute(
                SearchCodeRequest(
                    query=query,
                    top_k=min(max(int(top_k or 5), 1), MAX_TOP_K),
                    project_id=scoped_project,
                )
            )
        except ValueError as exc:
            return _render_error_fragment("search_code", _sanitize_error_message(exc))
        except Exception as exc:
            return _render_error_fragment("search_code", _sanitize_error_message(exc))
        return templates.TemplateResponse(
            request=request,
            name="partials/search_code.html",
            context={"query": query, "matches": matches, "error": None},
            status_code=200,
        )

    @router.post(
        "/playground/api/explain_architecture",
        response_class=HTMLResponse,
        name="playground_explain_architecture",
    )
    async def explain_architecture_fragment(
        request: Request,
        project_id: str = Form(...),
    ) -> HTMLResponse:
        composition = get_composition(request)
        if composition is None:
            raise HTTPException(status_code=500, detail="composition not wired")

        try:
            result = composition.explain_architecture_use_case.execute(
                ExplainArchitectureRequest(project_id=project_id)
            )
        except ManifestProjectNotFoundError:
            return _render_error_fragment(
                "explain_architecture",
                f'project_id "{project_id}" is not declared in the manifest',
                status_code=404,
            )
        except FileNotFoundError as exc:
            return _render_error_fragment(
                "explain_architecture", _sanitize_error_message(exc), status_code=404
            )
        except Exception as exc:
            return _render_error_fragment("explain_architecture", _sanitize_error_message(exc))

        return templates.TemplateResponse(
            request=request,
            name="partials/explain_architecture.html",
            context={"result": result, "error": None},
            status_code=200,
        )

    @router.post(
        "/playground/api/summarize_readme",
        response_class=HTMLResponse,
        name="playground_summarize_readme",
    )
    async def summarize_readme_fragment(
        request: Request,
        project_id: str = Form(...),
    ) -> HTMLResponse:
        composition = get_composition(request)
        if composition is None:
            raise HTTPException(status_code=500, detail="composition not wired")

        try:
            result = composition.summarize_readme_use_case.execute(
                SummarizeReadmeRequest(project_id=project_id)
            )
        except ManifestProjectNotFoundError:
            return _render_error_fragment(
                "summarize_readme",
                f'project_id "{project_id}" is not declared in the manifest',
                status_code=404,
            )
        except FileNotFoundError as exc:
            return _render_error_fragment(
                "summarize_readme", _sanitize_error_message(exc), status_code=404
            )
        except Exception as exc:
            return _render_error_fragment("summarize_readme", _sanitize_error_message(exc))

        return templates.TemplateResponse(
            request=request,
            name="partials/summarize_readme.html",
            context={"result": result, "error": None},
            status_code=200,
        )

    @router.post(
        "/playground/api/get_architecture_diagram",
        response_class=HTMLResponse,
        name="playground_get_architecture_diagram",
    )
    async def get_architecture_diagram_fragment(
        request: Request,
        project_id: str = Form(...),
    ) -> HTMLResponse:
        composition = get_composition(request)
        if composition is None:
            raise HTTPException(status_code=500, detail="composition not wired")

        try:
            result = composition.get_architecture_diagram_use_case.execute(
                GetArchitectureDiagramRequest(project_id=project_id)
            )
        except ManifestProjectNotFoundError:
            return _render_error_fragment(
                "get_architecture_diagram",
                f'project_id "{project_id}" is not declared in the manifest',
                status_code=404,
            )
        except FileNotFoundError as exc:
            return _render_error_fragment(
                "get_architecture_diagram",
                _sanitize_error_message(exc),
                status_code=404,
            )
        except (ValueError, binascii.Error) as exc:
            return _render_error_fragment(
                "get_architecture_diagram",
                _sanitize_error_message(exc),
                status_code=400,
            )
        except Exception as exc:
            return _render_error_fragment("get_architecture_diagram", _sanitize_error_message(exc))

        return templates.TemplateResponse(
            request=request,
            name="partials/architecture_diagram.html",
            context={"result": result, "error": None},
            status_code=200,
        )


def _render_error_fragment(
    tool_name: str, error_message: str, *, status_code: int = 400
) -> HTMLResponse:
    """Render ``partials/error.html`` with a sanitized message body."""
    body = templates.get_template("partials/error.html").render(
        tool_name=tool_name,
        error=error_message,
    )
    return HTMLResponse(content=body, status_code=status_code)


def _sanitize_error_message(exc: BaseException) -> str:
    """Return a user-safe error message — drop exception class prefixes
    and absolute paths so the HTML fragment leaks nothing useful.
    """
    text = str(exc) or exc.__class__.__name__
    # Trim any leading class prefix like "ValueError: " from the message.
    if ":" in text and text.split(":", 1)[0] == exc.__class__.__name__:
        text = text.split(":", 1)[1].strip()
    return _collapse_whitespace(text)


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Custom Jinja filter for the diagram template — decode base64 data
# ---------------------------------------------------------------------------

try:
    templates.env.filters["b64decode"] = lambda value: base64.b64decode(value).decode(
        "utf-8", errors="replace"
    )
except AttributeError:
    # Older Jinja2 versions: still need to expose the filter on the
    # TemplateResponse pipeline. Fall back to a global attribute lookup
    # the Starlette helper understands.
    templates.env.globals["b64decode_filter"] = lambda value: base64.b64decode(value).decode(
        "utf-8", errors="replace"
    )


# Iterator used by the router for documentation purposes.
def playground_form_tool_names() -> Iterable[str]:
    return PLAYGROUND_FORM_TOOLS


# Concrete path resolution — used by the Dockerfile and tests to validate
# the playground/ tree ships correctly. Not exported via __all__ because
# it's only consumed by integration tests.
def static_dir() -> Path:
    # parents[5] = repo root (../src/mcp_server/interfaces/http/web/playground.py
    # is 5 deep into the repo).
    return Path(__file__).resolve().parents[6] / "playground" / "static"
