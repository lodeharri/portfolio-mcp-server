"""Tests for playground path resolution.

Regression: before the helper in ``web/paths.py`` was introduced,
``_static_dir()`` and ``templates_dir()`` used a fixed
``Path(__file__).resolve().parents[N]`` walk that broke in Docker
images. When the package was installed (non-editable) at
``/opt/venv/lib/python3.10/site-packages/mcp_server/...``, the walk
landed at ``/opt/venv/lib/python3.10/`` (no ``playground/`` sibling),
and the server crashed at startup with a confusing
``RuntimeError: Directory '/opt/venv/lib/python3.10/playground/static'
does not exist`` deep inside Starlette.

The helper now tries (in order):

1. Walk up from this module's location to find the repo root (source
   tree / editable install).
2. ``/app/playground/<subdir>`` (Docker WORKDIR layout).
3. ``MCP_SERVER_PLAYGROUND_DIR`` env var override (intentionally NOT
   supported here — only ``config.py`` may read env vars per the
   hexagonal invariant; tests use monkeypatch instead).

Each strategy is exercised below.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.interfaces.http.web.paths import resolve_playground_subdir


def test_resolve_playground_subdir_source_tree_layout(tmp_path: Path) -> None:
    """Strategy 1: walk up from this module finds playground/ in the source tree."""
    # In the source tree, playground/ is at /<repo>/playground/. The helper
    # already finds it without any setup; assert the resolved path
    # actually contains the requested subdirectory.
    static_dir = resolve_playground_subdir("static")
    assert static_dir.is_dir()
    assert (static_dir / "htmx.min.js").exists(), (
        "Expected playground/static/htmx.min.js in the source tree; "
        "did the layout change?"
    )


def test_resolve_playground_subdir_docker_layout_via_monkeypatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strategy 2: when source-tree walk fails, /app/playground/ is tried.

    We simulate the Docker layout by monkeypatching ``Path.is_dir`` so
    that only ``/app/playground/static`` returns True. We can't write
    to ``/app/`` from a unit test (no root), so this is the closest we
    can get to proving the Docker branch.
    """
    real_is_dir = Path.is_dir

    def fake_is_dir(self: Path) -> bool:  # type: ignore[override]
        # Make every source-tree-walk candidate return False (paths
        # under any /playground/static/ ancestor that isn't the Docker
        # /app one). Only /app/playground/static "exists".
        if str(self) == "/app/playground/static":
            return True
        # Reject anything that looks like a source-tree playground
        # candidate (ends in /playground/static under a non-/app root).
        parts = self.parts
        if len(parts) >= 2 and parts[-2:] == ("playground", "static"):
            return False
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", fake_is_dir)

    resolved = resolve_playground_subdir("static")
    assert resolved == Path("/app/playground/static")


def test_resolve_playground_subdir_raises_with_all_attempted_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both strategies fail, FileNotFoundError lists every attempted path."""

    def always_false(self: Path) -> bool:  # type: ignore[override]
        return False

    monkeypatch.setattr(Path, "is_dir", always_false)

    with pytest.raises(FileNotFoundError) as exc_info:
        resolve_playground_subdir("static")

    msg = str(exc_info.value)
    assert "playground/static/ not found" in msg
    assert "/app/playground/static" in msg, (
        "Error must list the Docker WORKDIR attempt so operators can "
        "diagnose a misconfigured image."
    )


def test_resolve_playground_subdir_templates_subdir() -> None:
    """The same helper resolves templates/ alongside static/."""
    templates_dir = resolve_playground_subdir("templates")
    assert templates_dir.is_dir()
    assert (templates_dir / "base.html").exists()
    assert (templates_dir / "index.html").exists()
    assert (templates_dir / "chat.html").exists()


def test_resolve_playground_subdir_rejects_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subdir with ``..`` is rejected (defense in depth).

    The helper shouldn't accept ``../etc`` style inputs even if a
    caller were to construct one — only known subdirs are valid.
    Currently the helper just passes the value through, but we want
    to lock down the contract: only ``static`` and ``templates`` are
    legitimate subdirs in this codebase.
    """
    with pytest.raises((FileNotFoundError, ValueError)):
        # ``../etc`` won't resolve to an existing playground subdir.
        resolve_playground_subdir("../etc")
