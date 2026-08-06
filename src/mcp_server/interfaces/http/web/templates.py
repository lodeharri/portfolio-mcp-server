"""Shared Jinja2Templates environment for the playground.

The web router exposes a single :class:`Jinja2Templates` instance bound
to ``playground/templates/`` (sibling to the repo root). Every page
extends ``base.html`` so the navigation and asset references stay
consistent across ``GET /``, ``GET /playground``, and the future
``GET /chat``.

The templates directory is resolved relative to the repo root (not
relative to the package) so the Docker image's
``/app/playground/templates/`` layout works without any path
manipulation.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

__all__ = ["TEMPLATES_DIR", "templates", "templates_dir"]


def templates_dir() -> Path:
    """Return the absolute playground/templates/ directory.

    Walks up from this file to find the repo root (parent of ``src/``),
    then into ``playground/templates/``. Works for editable installs
    and for the Docker image whose layout is ``/app/playground/...``.
    """
    return Path(__file__).resolve().parents[5] / "playground" / "templates"


TEMPLATES_DIR: Path = templates_dir()
templates: Jinja2Templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
