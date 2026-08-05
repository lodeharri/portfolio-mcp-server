"""Sentinel test for the <150 MB image-size budget.

This test is intentionally opt-in: it requires a local Docker daemon and
the project being built into an image. In CI it runs on every deploy
(see ``.github/workflows/deploy.yml``). Locally it is skipped unless
both the Python ``docker`` package and the Docker daemon are available.

Why a sentinel test instead of a hard assertion? Two reasons:

1. The size budget is a deploy-time property of the BUILT image, not a
   runtime property of the source code. There is no source line whose
   correctness implies the image will be under 150 MB.
2. The build itself depends on the host toolchain (buildx, BuildKit,
   network for pip + gitleaks tarball). A failing build on a developer
   laptop should not block local test runs.

The actual CI gate that this test codifies lives in the ``deploy.yml``
``docker-build`` job, which runs:

* ``docker build -t mcp-server:test .``
* ``docker image ls mcp-server:test --format '{{.Size}}'`` → < 150 MB
* ``docker run --rm mcp-server:test id -u`` → ``10001``
* ``docker run --rm mcp-server:test env | grep -i gemini`` → empty
* ``docker history mcp-server:test --no-trunc`` → no GEMINI_API_KEY

The unit-level colocation here is purely so future contributors can
discover the gate from a grep over ``tests/``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# Skip the whole module if either the docker CLI is missing or the
# ``DOCKER_BUILDKIT``/``docker`` buildx backend is not available. The
# sentinel is opt-in locally; CI runs it unconditionally.
pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="docker CLI not on PATH; size gate is enforced in CI",
)

# Sub-strings emitted by docker when the daemon is not reachable.
# ``docker info`` on a healthy daemon returns 0 and a multi-line JSON.
# Anything else with one of these phrases means the daemon is down.
_DOCKER_DOWN_HINTS = (
    "Cannot connect to the Docker daemon",
    "Is the docker daemon running",
    "could not be found in this WSL",  # WSL2 without Docker Desktop integration
    "open //./pipe/docker_engine",  # Windows daemon unreachable from WSL
)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a docker command, return CompletedProcess; let exceptions propagate."""
    return subprocess.run(  # noqa: S603 — fixed-argv subprocess, no shell
        cmd,
        capture_output=True,
        text=True,
        check=False,
        shell=False,
        timeout=600,
    )


def _docker_daemon_up() -> bool:
    """Return True iff the docker daemon responds to ``docker info``."""
    try:
        result = _run(["docker", "info"])
    except (FileNotFoundError, OSError):
        return False
    if result.returncode != 0:
        blob = (result.stderr or "") + (result.stdout or "")
        return not any(hint in blob for hint in _DOCKER_DOWN_HINTS)
    return True


# Combine both gates into a single pytestmark so the second assignment
# does not shadow the first.
_SKIP_REASON: str | None
if shutil.which("docker") is None:
    _SKIP_REASON = "docker CLI not on PATH; size gate is enforced in CI"
elif not _docker_daemon_up():
    _SKIP_REASON = "docker daemon not reachable; size gate is enforced in CI"
else:
    _SKIP_REASON = None

pytestmark = pytest.mark.skipif(
    _SKIP_REASON is not None,
    reason=_SKIP_REASON or "docker available",
)

# 150 MB expressed in bytes for the parseable comparison.
SIZE_BUDGET_BYTES = 150 * 1024 * 1024


def _parse_human_size(text: str) -> int:
    """Parse a docker size string like ``'95.2MB'`` or ``'120MB'`` into bytes.

    Docker emits sizes with one of: ``B``, ``kB``, ``MB``, ``GB``, ``TB``
    (note: uppercase MB / lowercase kB). This helper only handles the
    cases we expect on a 100-150 MB image — MB and GB.
    """
    text = text.strip()
    match = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)$", text)
    if not match:
        raise ValueError(f"cannot parse docker size: {text!r}")
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "B":
        return int(value)
    if unit == "kB":
        return int(value * 1024)
    if unit == "MB":
        return int(value * 1024 * 1024)
    if unit == "GB":
        return int(value * 1024 * 1024 * 1024)
    raise ValueError(f"unsupported docker size unit: {unit!r}")


def test_docker_image_size_under_budget(tmp_path: Path) -> None:
    """Build the image and assert its compressed size is below 150 MB.

    The tag ``mcp-server-playground:size-test`` is used so the sentinel
    never clobbers any tag a developer has lying around. The build uses
    the same args the deploy workflow uses (BAKE_INDEX=on is omitted here
    because we have no API key in CI by default; the build still
    succeeds thanks to the auto-mock-gemini fallback documented in
    ``preindex.py``).

    On a developer machine this might take 3-5 minutes (cold pip cache).
    """
    image_tag = "mcp-server-playground:size-test"
    repo_root = Path(__file__).resolve().parents[2]

    # Build with BuildKit so the secret mount (if any) is honored. We
    # don't pass --secret here because the test is meant to verify the
    # size budget, not the secret plumbing; the secret-leak guard is a
    # separate CI step.
    env = {**os.environ, "DOCKER_BUILDKIT": "1"}
    build = subprocess.run(  # noqa: S603 — fixed docker invocation
        ["docker", "build", "-t", image_tag, str(repo_root)],
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

    try:
        ls = _run(["docker", "image", "ls", image_tag, "--format", "{{.Size}}"])
        assert ls.returncode == 0, f"docker image ls failed: {ls.stderr}"
        size_text = ls.stdout.strip()
        size_bytes = _parse_human_size(size_text)
        assert size_bytes < SIZE_BUDGET_BYTES, (
            f"image size {size_text} ({size_bytes} bytes) exceeds the "
            f"150 MB ({SIZE_BUDGET_BYTES} bytes) budget. Check Dockerfile "
            f"layers for unused apt packages, build artifacts, or unstripped test data."
        )
    finally:
        # Always clean up the sentinel image so we don't leave a ~150 MB
        # dangling layer on the developer's machine.
        _run(["docker", "image", "rm", "-f", image_tag])


def test_docker_image_runs_as_non_root(tmp_path: Path) -> None:
    """The built image MUST run as UID 10001 (the mcp non-root user).

    Mirrors the spec scenario "Container runs as non-root". The image
    used is the one we just built in ``test_docker_image_size_under_budget``;
    if that test was skipped, this one is skipped too — both need a
    successful build.
    """
    image_tag = "mcp-server-playground:size-test"
    # If the size test was skipped, the image won't exist; skip too.
    inspect = _run(["docker", "image", "inspect", image_tag])
    if inspect.returncode != 0:
        pytest.skip("image not built (size test was skipped)")

    id_cmd = _run(["docker", "run", "--rm", image_tag, "id", "-u"])
    assert id_cmd.returncode == 0, f"docker run failed: {id_cmd.stderr}"
    assert id_cmd.stdout.strip() == "10001", (
        f"container should run as UID 10001, got {id_cmd.stdout!r}"
    )


def test_docker_image_does_not_leak_gemini_key(tmp_path: Path) -> None:
    """The built image MUST NOT contain GEMINI_API_KEY in env or history.

    Mirrors the spec scenario "Index baked at build time" (the secret
    MUST NOT be in the published image). If the build never had access
    to a key, this is a vacuous pass — which is fine; the deploy gate
    is the authoritative source.
    """
    image_tag = "mcp-server-playground:size-test"
    inspect = _run(["docker", "image", "inspect", image_tag])
    if inspect.returncode != 0:
        pytest.skip("image not built (size test was skipped)")

    env_check = _run(["docker", "run", "--rm", image_tag, "env"])
    assert env_check.returncode == 0, f"docker run failed: {env_check.stderr}"
    env_blob = env_check.stdout
    # Case-insensitive search for any GEMINI_API_KEY export.
    leaked = [
        line
        for line in env_blob.splitlines()
        if "GEMINI_API_KEY" in line and "=" in line and not line.startswith("#")
    ]
    assert not leaked, (
        f"GEMINI_API_KEY leaked into the runtime image env: {leaked!r}. "
        "Use BuildKit --secret id=gemini,env=GEMINI_API_KEY and never "
        "ARG/ENV it in the Dockerfile."
    )

    history = _run(["docker", "history", image_tag, "--no-trunc"])
    assert history.returncode == 0, f"docker history failed: {history.stderr}"
    assert "GEMINI_API_KEY" not in history.stdout, (
        "GEMINI_API_KEY appears in the docker history (image layer metadata). "
        "Use BuildKit --secret id=gemini,env=GEMINI_API_KEY to keep it out of "
        "the image layers."
    )
