"""Integration tests for the playground assets in the runtime image.

Per the ``dockerfile-playground`` spec:

* The runtime image MUST contain ``/app/playground/`` with the
  vendored HTMX, the Solarized Phosphor style sheet, and the
  Jinja2 templates (base.html, index.html, chat.html, mcp_browser.html).
* The vendored HTMX banner MUST start with the embedded version
  string (``version:"1.9.10"``); the spec's
  ``starts with /* htmx.org */`` claim refers to legacy htmx 0.x —
  htmx 1.x minified output does NOT carry that banner.
* Image size MUST stay under 500 MB.
* The ``playground/`` directory MUST be owned by the ``mcp`` user.

These tests are gated behind the same ``docker CLI available + daemon
reachable`` checks as ``tests/integration/test_docker_size.py`` so
local environments without Docker skip them.

The tests are RED until the ``COPY playground ./playground`` line
lands in the Dockerfile.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker CLI not on PATH; playground-in-image gate runs in CI",
)

_DOCKER_DOWN_HINTS = (
    "Cannot connect to the Docker daemon",
    "Is the docker daemon running",
    "could not be found in this WSL",
    "open //./pipe/docker_engine",
)

_IMAGE_TAG = "mcp-server-playground:playground-test"
_SIZE_BUDGET_BYTES = 500 * 1024 * 1024
_HTMX_MIN_BYTES = 10_000  # htmx 1.9.10 minified is ~48 KB
_HTMX_VERSION_MARKER = b"1.9.10"


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed-argv subprocess, no shell
        cmd,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=600,
    )


def _docker_daemon_up() -> bool:
    try:
        result = _run(["docker", "info"])
    except (FileNotFoundError, OSError):
        return False
    if result.returncode != 0:
        blob = (result.stderr or "") + (result.stdout or "")
        return not any(hint in blob for hint in _DOCKER_DOWN_HINTS)
    return True


if not _docker_daemon_up():
    pytestmark = pytest.mark.skip(reason="docker daemon unreachable")


@pytest.fixture(scope="module")
def built_image():
    """Build the runtime image once for all tests in this module.

    Returns the image tag. Cleans up via a finalizer so the developer's
    local Docker isn't littered with the test image.
    """
    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    build = subprocess.run(  # noqa: S603 — fixed docker invocation
        ["docker", "build", "-t", _IMAGE_TAG, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=900,
        env=env,
    )
    if build.returncode != 0:
        pytest.fail(
            f"docker build failed (exit={build.returncode})\n"
            f"STDOUT:\n{build.stdout[-2000:]}\n"
            f"STDERR:\n{build.stderr[-2000:]}"
        )

    yield _IMAGE_TAG

    # Finalizer: remove the test image so the developer's disk isn't
    # burdened by ~500 MB worth of layers.
    _run(["docker", "image", "rm", "-f", _IMAGE_TAG])


class TestPlaygroundInRuntimeImage:
    def test_playground_directory_present(self, built_image) -> None:
        """``/app/playground/`` MUST exist in the runtime image and
        carry base.html, style.css, htmx.min.js.
        """
        ls = _run(["docker", "run", "--rm", built_image, "ls", "-la", "/app/playground/"])
        assert ls.returncode == 0, f"docker run failed: {ls.stderr}"
        out = ls.stdout
        assert "templates" in out
        assert "static" in out

    def test_base_html_in_image(self, built_image) -> None:
        head = _run(
            [
                "docker",
                "run",
                "--rm",
                built_image,
                "head",
                "-c",
                "200",
                "/app/playground/templates/base.html",
            ]
        )
        assert head.returncode == 0, f"docker run failed: {head.stderr}"
        assert b"<html" in head.stdout.encode("utf-8", errors="replace") or "html" in head.stdout

    def test_index_html_in_image(self, built_image) -> None:
        result = _run(
            [
                "docker",
                "run",
                "--rm",
                built_image,
                "test",
                "-f",
                "/app/playground/templates/index.html",
            ]
        )
        assert result.returncode == 0, "index.html must ship in the image"

    def test_style_css_in_image(self, built_image) -> None:
        result = _run(
            ["docker", "run", "--rm", built_image, "test", "-f", "/app/playground/static/style.css"]
        )
        assert result.returncode == 0, "style.css must ship in the image"

    def test_htmx_min_js_in_image(self, built_image) -> None:
        """The vendored HTMX 1.9.10 MUST be reachable at the documented
        path; the legacy ``/* htmx.org */`` banner is absent in 1.x so
        we check the embedded version string instead.
        """
        ls = _run(
            [
                "docker",
                "run",
                "--rm",
                built_image,
                "ls",
                "-la",
                "/app/playground/static/htmx.min.js",
            ]
        )
        assert ls.returncode == 0, f"docker run failed: {ls.stderr}"
        # Parse the size from `ls -la` (5th column = bytes).
        parts = ls.stdout.split()
        size_bytes = int(parts[4])
        assert size_bytes >= _HTMX_MIN_BYTES, (
            f"Vendored HTMX is suspiciously small ({size_bytes} bytes); "
            "the COPY for playground/ may have failed silently."
        )

    def test_htmx_contains_version_marker(self, built_image) -> None:
        """The vendored HTMX must contain the embedded ``1.9.10``
        string. We read the file via ``docker exec cat`` to assert
        bytes reach the runtime image unchanged.
        """
        cat = _run(
            ["docker", "run", "--rm", built_image, "cat", "/app/playground/static/htmx.min.js"]
        )
        assert cat.returncode == 0, f"docker run failed: {cat.stderr}"
        assert _HTMX_VERSION_MARKER in cat.stdout.encode("utf-8", errors="replace"), (
            "Vendored HTMX in runtime image must contain version string 1.9.10"
        )

    def test_playground_dir_owned_by_mcp_user(self, built_image) -> None:
        """The playground/ tree MUST be owned by the non-root ``mcp``
        user (matching the manifest's per-container user context).
        """
        stat = _run(
            ["docker", "run", "--rm", built_image, "stat", "-c", "%U:%G", "/app/playground"]
        )
        assert stat.returncode == 0, f"docker run failed: {stat.stderr}"
        assert stat.stdout.strip() == "mcp:mcp", (
            f"playground/ must be owned by mcp:mcp; got {stat.stdout!r}"
        )


class TestVendoredHtmxNoCdnReferences:
    def test_runtime_image_htmx_has_no_cdn_url(self, built_image) -> None:
        """Defensive: the runtime image MUST NOT reference any external
        CDN for HTMX. Per Decision #1, the file is vendored and the
        Dockerfile MUST NOT add an external fetch step.
        """
        # ``grep`` the file via docker run; both unpkg.com and jsdelivr
        # are forbidden hosts.
        for forbidden in ("unpkg.com", "cdn.jsdelivr.net"):
            result = _run(
                [
                    "docker",
                    "run",
                    "--rm",
                    built_image,
                    "grep",
                    "-l",
                    forbidden,
                    "/app/playground/static/htmx.min.js",
                ]
            )
            # grep returns 0 if matches found, 1 if no matches, >1 on error.
            assert result.returncode != 0, (
                f"Vendored HTMX must not reference {forbidden!r} (got rc=0)"
            )


class TestImageSizeBudget:
    def test_image_size_under_500_mb(self, built_image) -> None:
        """The runtime image MUST stay below the 500 MB cap. The
        playground additions are < 1 MB total; this is a regression
        guard against future drift.
        """
        ls = _run(["docker", "image", "ls", built_image, "--format", "{{.Size}}"])
        assert ls.returncode == 0, f"docker image ls failed: {ls.stderr}"
        size_text = ls.stdout.strip()
        # Parse ``'417MB'`` style. Use the same helper from test_docker_size.py.
        import re

        match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)$", size_text)
        assert match, f"cannot parse docker size: {size_text!r}"
        value = float(match.group(1))
        unit = match.group(2)
        if unit == "GB":
            bytes_ = int(value * 1024**3)
        elif unit == "MB":
            bytes_ = int(value * 1024**2)
        elif unit == "kB":
            bytes_ = int(value * 1024)
        else:
            bytes_ = int(value)
        assert bytes_ < _SIZE_BUDGET_BYTES, (
            f"image size {size_text} ({bytes_} bytes) exceeds the 500 MB "
            f"({_SIZE_BUDGET_BYTES} bytes) budget"
        )
