"""Shared Jinja2Templates environment for the playground.

The web router exposes a single :class:`Jinja2Templates` instance bound
to ``playground/templates/`` (sibling to the repo root). Every page
extends ``base.html`` so the navigation and asset references stay
consistent across ``GET /``, ``GET /playground``, and
``GET /chat``.

The templates directory is resolved through
:func:`mcp_server.interfaces.http.web.paths.resolve_playground_subdir`
which handles source-tree walks (editable installs), the Docker
WORKDIR layout (``/app/playground/...``), and the
``MCP_SERVER_PLAYGROUND_DIR`` env var override.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from mcp_server.interfaces.http.web.paths import resolve_playground_subdir

__all__ = ["TEMPLATES_DIR", "templates", "templates_dir"]


def templates_dir() -> Path:
    """Return the absolute playground/templates/ directory.

    Delegates to :func:`resolve_playground_subdir` (see ``web/paths.py``
    for the resolution strategy).
    """
    return resolve_playground_subdir("templates")


TEMPLATES_DIR: Path = templates_dir()
templates: Jinja2Templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
