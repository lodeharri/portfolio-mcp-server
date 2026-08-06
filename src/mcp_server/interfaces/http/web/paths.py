"""Path resolution for the playground web UI.

The playground assets (``playground/static/`` and ``playground/templates/``)
live at the **repo root**, not inside the Python package. At runtime the
package can be in two very different locations:

1. **Source tree / editable install** — ``src/mcp_server/interfaces/http/web/...``
   is inside the repo at ``/path/to/mcp-server-playground/src/...`` and the
   playground tree is at ``/path/to/mcp-server-playground/playground/``.
   ``Path(__file__).resolve().parents[N] / "playground" / <subdir>`` walks
   up to the repo root and finds it.

2. **Docker image (non-editable install)** — ``pip install .`` copies
   the package to ``/opt/venv/lib/python3.10/site-packages/mcp_server/...``
   while the playground tree is at ``/app/playground/...`` (the
   ``WORKDIR`` in the Dockerfile). ``parents[N]`` from the install
   location lands at ``/opt/venv/lib/python3.10/``, not ``/app/``, so
   the simple walk-up fails. The Docker image's WORKDIR layout must be
   recognized explicitly.

The :func:`resolve_playground_subdir` helper tries both layouts in
order and returns the first match. It raises a clear error if neither
resolves, so operators see actionable feedback instead of a confusing
``RuntimeError: Directory '/opt/venv/lib/python3.10/playground/static' does
not exist`` deep inside Starlette.

Per the project's hexagonal invariant, only ``src/mcp_server/config.py``
may read ``os.environ``. This module deliberately does NOT read env vars
directly; ``AppConfig.playground_dir`` (set by config.py from
``MCP_SERVER_PLAYGROUND_DIR``) is the supported override path for
unusual deployment layouts.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["resolve_playground_subdir"]


def resolve_playground_subdir(subdir: str) -> Path:
    """Return the absolute ``playground/<subdir>/`` directory.

    Args:
        subdir: The subdirectory under ``playground/``. Typically
            ``"static"`` or ``"templates"``.

    Returns:
        The resolved absolute path. Guaranteed to exist on disk.

    Raises:
        FileNotFoundError: If no resolution strategy produces an
            existing directory. The error message names every strategy
            attempted so the operator can diagnose a misconfigured
            install or image.
    """
    attempted: list[Path] = []

    # Strategy 1: walk up from this module's location to find the repo root.
    # Source tree: /<repo>/src/mcp_server/web/paths.py → parents[4] = /<repo>/
    # Editable install: same as source tree if the install points at src/.
    here = Path(__file__).resolve()
    for depth in range(2, 8):
        candidate = here.parents[depth] / "playground" / subdir
        attempted.append(candidate)
        if candidate.is_dir():
            return candidate

    # Strategy 2: Docker WORKDIR layout (non-editable install inside an image).
    # The Dockerfile sets WORKDIR /app and COPY playground ./playground there.
    docker_candidate = Path("/app/playground") / subdir
    attempted.append(docker_candidate)
    if docker_candidate.is_dir():
        return docker_candidate

    raise FileNotFoundError(
        f"playground/{subdir}/ not found. Attempted: "
        + ", ".join(str(p) for p in attempted)
        + ". Verify the Docker COPY of playground/ landed at /app/playground/, "
        "or check that this package is installed in editable mode pointing at "
        "the repo's src/ tree."
    )
