"""Typed application configuration — single source of env-var access.

This module is the ONLY place in ``src/mcp_server/`` that may read
``os.environ``. Any other module that needs configuration MUST go through
``AppConfig`` or ``load_config()``. The hexagonal invariant test enforces
this — see ``tests/integration/test_hexagonal_invariants.py`` and
``openspec/config.yaml``.

Two types live here:

* :class:`BuildInfo` — build-time metadata embedded in the container at
  build time (``COMMIT_SHA``, ``BUILT_AT``, ``VERSION``). Populated at
  module import time from env vars via :func:`build_info_from_env`.

* :class:`AppConfig` — runtime configuration. ``load_config()`` reads env
  vars once and returns a fresh validated instance.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

# Load .env file at module import time — but ONLY outside test runs.
# During tests we want deterministic behaviour (mock-gemini by default),
# not the user's local `.env` overwriting test env vars. Pytest sets
# `pytest` in `sys.modules` long before this module is imported, so the
# check is reliable.
import sys
if "pytest" not in sys.modules:
    load_dotenv()

# ---------------------------------------------------------------------------
# Build-time defaults
# ---------------------------------------------------------------------------

#: Fallback commit SHA when ``COMMIT_SHA`` env var is unset (i.e. local dev).
_DEFAULT_COMMIT_SHA: Final[str] = "dev"

#: Fallback version when ``importlib.metadata`` cannot resolve the package
#: (e.g. running from a source checkout without ``pip install -e``).
_DEFAULT_VERSION: Final[str] = "0.1.0"


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _package_version() -> str:
    """Read the package version via :mod:`importlib.metadata`.

    Falls back to :data:`_DEFAULT_VERSION` if the package is not installed
    (e.g. during a source-only smoke test).
    """
    try:
        return importlib_metadata.version("mcp-server-playground")
    except importlib_metadata.PackageNotFoundError:
        return _DEFAULT_VERSION


# ---------------------------------------------------------------------------
# BuildInfo
# ---------------------------------------------------------------------------


class BuildInfo(BaseModel):
    """Build-time metadata embedded in the container at build time.

    Fields:
        commit_sha: git commit SHA of the build (default ``"dev"``).
        built_at: ISO-8601 timestamp of the build (default = time of
            construction; ``"now"`` semantically per the PR1 spec).
        version: package version, read from ``importlib.metadata`` with a
            fallback of ``"0.1.0"``.

    The defaults match the orchestrator PR1 spec: ``dev``/``now``/``0.1.0``.
    The module-level :data:`BUILD_INFO` constant overrides these from the
    ``COMMIT_SHA``/``BUILT_AT``/``VERSION`` env vars at import time via
    :func:`build_info_from_env`.
    """

    commit_sha: str = _DEFAULT_COMMIT_SHA
    #: ``default_factory=_now_iso`` so each fresh instance captures the
    #: moment it was built. Tests can construct with ``built_at="now"``.
    built_at: str = Field(default_factory=_now_iso)
    version: str = Field(default_factory=_package_version)


def build_info_from_env() -> BuildInfo:
    """Build a fresh :class:`BuildInfo` from the current process env vars.

    Exposed as a function (not just a module-level constant) so tests can
    exercise the env-var mapping with :func:`pytest.MonkeyPatch.setenv`
    without triggering ``importlib.reload`` (which creates a new class
    object and breaks ``isinstance`` checks elsewhere in the suite).
    """
    return BuildInfo(
        commit_sha=os.environ.get("COMMIT_SHA", _DEFAULT_COMMIT_SHA),
        built_at=os.environ.get("BUILT_AT", _now_iso()),
        version=os.environ.get("VERSION", _package_version()),
    )


# Constructed at import time per the PR1 spec. Reads COMMIT_SHA, BUILT_AT,
# VERSION from env vars; falls back to BuildInfo defaults.
BUILD_INFO: Final[BuildInfo] = build_info_from_env()


# ---------------------------------------------------------------------------
# AppConfig — runtime configuration
# ---------------------------------------------------------------------------

_DEFAULT_PORT: Final[int] = 8080
_DEFAULT_EMBEDDING_DIM: Final[int] = 768
_DEFAULT_MANIFEST_PATH: Final[Path] = Path("config/projects.manifest.yaml")
_DEFAULT_DATA_DIR: Final[Path] = Path("data")


class AppConfig(BaseModel):
    """Typed application configuration.

    All fields have sensible defaults for local development. Production
    deployments override them via env vars through :func:`load_config`.

    ``build_info`` defaults to the module-level :data:`BUILD_INFO` constant
    populated at import time — callers rarely need to override it.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    port: int = _DEFAULT_PORT
    gemini_api_key: str | None = None
    embedding_dim: int = _DEFAULT_EMBEDDING_DIM
    manifest_path: Path = _DEFAULT_MANIFEST_PATH
    data_dir: Path = _DEFAULT_DATA_DIR
    build_info: BuildInfo = Field(default_factory=lambda: BUILD_INFO)
    # Private override attribute used only by the preindex CLI to thread
    # a ``--db PATH`` flag through configuration without re-introducing
    # env reads. Not part of the public schema; composition reads it via
    # ``getattr``. The ``extra="ignore"`` model_config above lets us set
    # this via ``model_copy(update={"_db_path_override": ...})`` without
    # pydantic rejecting the unknown attribute.
    _db_path_override: Path | None = None


# ---------------------------------------------------------------------------
# load_config — the ONLY function that reads os.environ at runtime
# ---------------------------------------------------------------------------


def load_config() -> AppConfig:
    """Read env vars once and return a validated :class:`AppConfig`.

    Env vars consumed (all optional):

    * ``PORT`` — ``int`` (default ``8080``)
    * ``GEMINI_API_KEY`` — ``str | None`` (default ``None``)
    * ``EMBEDDING_DIM`` — ``int`` (default ``768``)
    * ``MANIFEST_PATH`` — path string (default ``config/projects.manifest.yaml``)
    * ``DATA_DIR`` — path string (default ``data``)

    ``COMMIT_SHA``/``BUILT_AT``/``VERSION`` are read at module import time
    and live in :data:`BUILD_INFO`.

    Raises:
        pydantic.ValidationError: when any env var has an invalid type
            (e.g. ``PORT=abc``). The error wraps the underlying Pydantic
            coercion failure so callers can detect bad config without
            catching low-level ``ValueError``.
    """
    overrides: dict[str, object] = {}
    if "PORT" in os.environ:
        # Pass the raw string — Pydantic v2 raises ValidationError on bad int.
        overrides["port"] = os.environ["PORT"]
    if "GEMINI_API_KEY" in os.environ:
        overrides["gemini_api_key"] = os.environ["GEMINI_API_KEY"]
    if "EMBEDDING_DIM" in os.environ:
        overrides["embedding_dim"] = os.environ["EMBEDDING_DIM"]
    if "MANIFEST_PATH" in os.environ:
        overrides["manifest_path"] = os.environ["MANIFEST_PATH"]
    if "DATA_DIR" in os.environ:
        overrides["data_dir"] = os.environ["DATA_DIR"]
    return AppConfig(**overrides)


__all__ = [
    "BUILD_INFO",
    "AppConfig",
    "BuildInfo",
    "build_info_from_env",
    "load_config",
]
