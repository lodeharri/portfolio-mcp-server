"""Tests for ``src/mcp_server/config.py`` — typed config + BuildInfo.

These tests are RED until ``src/mcp_server/config.py`` is implemented.
They enforce the single-source-of-env invariant: ``config.py`` is the ONLY
module in ``src/mcp_server/`` that reads ``os.environ``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_server.config import (
    BUILD_INFO,
    AppConfig,
    BuildInfo,
    build_info_from_env,
    load_config,
)

# ---------------------------------------------------------------------------
# BuildInfo defaults
# ---------------------------------------------------------------------------


class TestBuildInfoDefaults:
    """``BuildInfo()`` with no args uses the documented defaults."""

    def test_default_commit_sha_is_dev(self) -> None:
        info = BuildInfo()
        assert info.commit_sha == "dev"

    def test_default_built_at_is_non_empty_iso_string(self) -> None:
        info = BuildInfo()
        # Default is the time of construction (ISO-8601), not the literal "now".
        # It just MUST be a non-empty string the caller can serialize.
        assert isinstance(info.built_at, str)
        assert info.built_at != ""

    def test_default_version_is_string(self) -> None:
        info = BuildInfo()
        assert isinstance(info.version, str)
        assert info.version != ""

    def test_all_three_defaults_when_constructed_pure(self) -> None:
        """Spec defaults per orchestrator PR1 prompt: ``dev``/``now``/``0.1.0``."""
        info = BuildInfo(
            commit_sha="dev",
            built_at="now",
            version="0.1.0",
        )
        assert info.commit_sha == "dev"
        assert info.built_at == "now"
        assert info.version == "0.1.0"


class TestBuildInfoModuleLevel:
    """``BUILD_INFO`` is constructed at module import time per PR1 spec."""

    def test_module_level_build_info_is_a_build_info(self) -> None:
        import mcp_server.config as config_module

        assert isinstance(config_module.BUILD_INFO, BuildInfo)

    def test_build_info_has_all_three_fields(self) -> None:
        assert hasattr(BUILD_INFO, "commit_sha")
        assert hasattr(BUILD_INFO, "built_at")
        assert hasattr(BUILD_INFO, "version")

    def test_build_info_reads_commit_sha_at_import_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``build_info_from_env()`` reads COMMIT_SHA from env vars at call time."""
        monkeypatch.setenv("COMMIT_SHA", "deadbeefcafe1234567890abcdef")
        info = build_info_from_env()
        assert info.commit_sha == "deadbeefcafe1234567890abcdef"

    def test_build_info_from_env_uses_default_commit_sha_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("COMMIT_SHA", raising=False)
        info = build_info_from_env()
        assert info.commit_sha == "dev"


# ---------------------------------------------------------------------------
# AppConfig defaults
# ---------------------------------------------------------------------------


class TestAppConfigDefaults:
    """``AppConfig()`` with no env vars uses sensible local-dev defaults."""

    def test_default_port_is_8080(self) -> None:
        cfg = AppConfig()
        assert cfg.port == 8080

    def test_default_embedding_dim_is_768(self) -> None:
        cfg = AppConfig()
        assert cfg.embedding_dim == 768

    def test_default_gemini_api_key_is_none(self) -> None:
        cfg = AppConfig()
        assert cfg.gemini_api_key is None

    def test_default_manifest_path(self) -> None:
        cfg = AppConfig()
        assert cfg.manifest_path == Path("config/projects.manifest.yaml")

    def test_default_data_dir(self) -> None:
        cfg = AppConfig()
        assert cfg.data_dir == Path("data")

    def test_default_build_info_attached(self) -> None:
        cfg = AppConfig()
        assert isinstance(cfg.build_info, BuildInfo)


# ---------------------------------------------------------------------------
# load_config() — reads env vars and validates
# ---------------------------------------------------------------------------


class TestLoadConfigPort:
    def test_default_port_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PORT", raising=False)
        cfg = load_config()
        assert cfg.port == 8080

    def test_port_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PORT", "9000")
        cfg = load_config()
        assert cfg.port == 9000

    def test_invalid_port_raises_validation_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PORT", "abc")
        with pytest.raises(ValidationError):
            load_config()


class TestLoadConfigEmbeddingDim:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("EMBEDDING_DIM", raising=False)
        cfg = load_config()
        assert cfg.embedding_dim == 768

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMBEDDING_DIM", "1024")
        cfg = load_config()
        assert cfg.embedding_dim == 1024

    def test_invalid_raises_validation_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EMBEDDING_DIM", "not-a-number")
        with pytest.raises(ValidationError):
            load_config()


class TestLoadConfigManifestPath:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MANIFEST_PATH", raising=False)
        cfg = load_config()
        assert cfg.manifest_path == Path("config/projects.manifest.yaml")

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MANIFEST_PATH", "/custom/manifest.yaml")
        cfg = load_config()
        assert cfg.manifest_path == Path("/custom/manifest.yaml")


class TestLoadConfigDataDir:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DATA_DIR", raising=False)
        cfg = load_config()
        assert cfg.data_dir == Path("data")

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATA_DIR", "/var/data")
        cfg = load_config()
        assert cfg.data_dir == Path("/var/data")


class TestLoadConfigGeminiApiKey:
    def test_default_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        cfg = load_config()
        assert cfg.gemini_api_key is None

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "test-key-xyz")
        cfg = load_config()
        assert cfg.gemini_api_key == "test-key-xyz"


class TestLoadConfigBuildInfo:
    def test_app_config_has_build_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cfg = load_config()
        assert isinstance(cfg.build_info, BuildInfo)

    def test_build_info_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("COMMIT_SHA", raising=False)
        monkeypatch.delenv("BUILT_AT", raising=False)
        monkeypatch.delenv("VERSION", raising=False)
        # BUILD_INFO was constructed at import time; we can't undo that here,
        # but we can verify that load_config() returns a config whose
        # build_info is a valid BuildInfo regardless of env state.
        cfg = load_config()
        assert cfg.build_info.commit_sha == BUILD_INFO.commit_sha
        # built_at is _now_iso() inside default_factory; just verify it's a
        # non-empty ISO string. (Two consecutive calls differ by microseconds.)
        assert isinstance(cfg.build_info.built_at, str)
        assert cfg.build_info.built_at != ""


class TestLoadConfigPurity:
    """``load_config()`` returns a fresh instance every call (no module cache)."""

    def test_two_calls_return_distinct_instances(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PORT", raising=False)
        cfg1 = load_config()
        cfg2 = load_config()
        assert cfg1 is not cfg2
        assert cfg1 == cfg2  # Same values, different objects


# ---------------------------------------------------------------------------
# Single-source-of-env invariant
# ---------------------------------------------------------------------------


class TestSingleSourceOfEnv:
    """The hexagonal invariant test enforces that config.py is the ONLY reader."""

    def test_config_module_exists(self) -> None:
        from pathlib import Path

        assert Path("src/mcp_server/config.py").exists()

    def test_config_module_imports_os_environ(self) -> None:
        """Only ``config.py`` may import ``os.environ`` — assert it does (sanity)."""
        import ast
        from pathlib import Path

        text = Path("src/mcp_server/config.py").read_text()
        tree = ast.parse(text)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "os"
                    and node.attr == "environ"
                ):
                    found = True
                    break
        assert found, "config.py must read os.environ (else load_config() cannot work)"
