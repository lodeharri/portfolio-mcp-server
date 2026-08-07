"""Regression tests for the manifest path-scoping filter.

Pre-2026-08-06 the preindex pipeline burned ~1000 Gemini embedding
requests on bundled/auto-generated code (``dist/`` and
``coverage/``) because :func:`YamlManifestAdapter._under_any_global`
compared the manifest's glob patterns (``**/dist/**``) as literal
strings against the path's segments. The literal ``**/dist/**`` is
never a single path segment, so every path was "accepted" by the
global exclude filter.

These tests pin the fix at the unit level — no real Gemini calls, no
filesystem walking, no DB writes. The same six cases that previously
demonstrated the bug are now asserted as the spec.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.config import AppConfig
from mcp_server.infrastructure.adapters.yaml_manifest import YamlManifestAdapter

# Reusable absolute paths against the two declared sibling projects.
# We do NOT touch the filesystem; ``is_path_indexed`` resolves the path
# string but tolerates non-existent parents via ``strict=False``.
FINCO_PATH = "/home/harri/development/projects/portfolio/finance-coach-latam"
LANDING_PATH = "/home/harri/development/projects/portfolio/landing-page-portfolio"


def _manifest() -> YamlManifestAdapter:
    return YamlManifestAdapter(AppConfig().manifest_path)


@pytest.mark.parametrize(
    ("path", "should_index"),
    [
        # The pre-fix bug repro: every one of these was ACCEPTED because
        # the literal glob strings ``**/dist/**`` / ``**/coverage/**`` /
        # ``**/node_modules/**`` did not match any single path segment.
        (f"{FINCO_PATH}/backend/dist/api/handler.js", False),
        (f"{FINCO_PATH}/backend/dist/lambda.js", False),
        (f"{FINCO_PATH}/backend/coverage/block-navigation.js", False),
        (f"{FINCO_PATH}/backend/coverage/prettify.js", False),
        (f"{FINCO_PATH}/backend/node_modules/some-pkg/index.js", False),
        (f"{FINCO_PATH}/backend/.aws/lambda.zip", False),
        # Source code that MUST be indexed (positive cases).
        (f"{FINCO_PATH}/backend/src/auth.py", True),
        (f"{FINCO_PATH}/backend/api/handler.ts", True),
        (f"{FINCO_PATH}/frontend/src/App.tsx", True),
        (f"{FINCO_PATH}/frontend/components/Hero.tsx", True),
        # Note: README.md at the project root is NOT indexed because
        # ``include_subdirs: [backend, frontend, infra]`` restricts the
        # walk. Recruiter demos use the manifest's ``readme_path`` field
        # to surface the README separately.
        # landing-page-portfolio: include_extensions added .astro
        # alongside the existing allow-list.
        (f"{LANDING_PATH}/src/components/Hero.astro", True),
        (f"{LANDING_PATH}/src/pages/index.astro", True),
        (f"{LANDING_PATH}/public/favicon.svg", False),  # .svg not in allow-list
        # Files OUTSIDE any declared project's path → False (default deny).
        ("/tmp/some-other-project/main.py", False),
    ],
)
def test_is_path_indexed_respects_glob_exclude_paths(path: str, should_index: bool) -> None:
    """The global ``exclude_paths`` globs MUST match at any depth.

    Regression: prior to the fix, every path that contained a
    ``/dist/``, ``/coverage/``, or ``/node_modules/`` segment was
    wrongly accepted because the literal ``**/dist/**`` was compared
    as a single segment. The preindex then embedded hundreds of
    bundled JS chunks instead of source code.
    """
    assert _manifest().is_path_indexed(Path(path)) is should_index, (
        f"is_path_indexed({path!r}) returned {not should_index}; expected {should_index}"
    )


def test_under_any_global_handles_both_plain_and_glob_tokens() -> None:
    """The helper must accept both ``node_modules`` and ``**/node_modules/**``."""
    from mcp_server.infrastructure.adapters.yaml_manifest import (
        YamlManifestAdapter,
    )

    resolved = Path("/home/x/proj/backend/node_modules/leak.py")

    # Plain token
    assert YamlManifestAdapter._under_any_global(resolved, ["node_modules"]) is True

    # Glob token (manifest style)
    assert YamlManifestAdapter._under_any_global(resolved, ["**/node_modules/**"]) is True

    # Both styles
    assert (
        YamlManifestAdapter._under_any_global(resolved, ["tmp", "**/node_modules/**", "**/dist/**"])
        is True
    )

    # Negative case
    assert YamlManifestAdapter._under_any_global(resolved, ["tmp", "**/dist/**"]) is False


def test_under_any_global_empty_prefixes_returns_false() -> None:
    """Defensive: empty / whitespace-only prefix lists never match."""
    from mcp_server.infrastructure.adapters.yaml_manifest import (
        YamlManifestAdapter,
    )

    resolved = Path("/home/x/proj/backend/dist/foo.js")

    assert YamlManifestAdapter._under_any_global(resolved, []) is False
    assert YamlManifestAdapter._under_any_global(resolved, [""]) is False
    assert YamlManifestAdapter._under_any_global(resolved, ["  "]) is False
