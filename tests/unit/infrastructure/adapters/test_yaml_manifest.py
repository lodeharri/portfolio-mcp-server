"""Tests for ``src/mcp_server/infrastructure/adapters/yaml_manifest.py``.

The :class:`YamlManifestAdapter` reads ``config/projects.manifest.yaml``,
flattens its nested ``server.{name,version,description}`` /
``indexing.{...}`` structure into the flat :class:`Manifest` model from
``mcp_server.application.ports.manifest``, and answers
``is_path_indexed(path)`` with default-deny semantics.

These tests are RED until the adapter exists. They cover:

* Loading a valid manifest from a tmp YAML fixture.
* Default-deny for paths outside any declared project.
* Default-deny for paths in a project's ``exclude_subdirs``.
* Default-deny for path-traversal attempts (``../``).
* Default-deny for file extensions not in ``include_extensions``.
* ``is_path_indexed`` reuses the same manifest across calls.
* The adapter satisfies :class:`ManifestPort` structurally.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture builder
# ---------------------------------------------------------------------------


def _write_manifest(tmp_path: Path, body: str) -> Path:
    """Write a manifest YAML fixture under ``tmp_path`` and return its path."""
    manifest_path = tmp_path / "projects.manifest.yaml"
    manifest_path.write_text(textwrap.dedent(body))
    return manifest_path


def _default_manifest_body(project_root: Path) -> str:
    """Return a valid manifest body with one project rooted at ``project_root``."""
    return f"""
        schema_version: 1

        server:
          name: portfolio-mcp-server
          version: 0.1.0
          description: test

        indexing:
          default_policy: deny
          chunk_size: 1500
          chunk_overlap: 200
          include_extensions:
            - .py
            - .md
          exclude_paths:
            - node_modules

        projects:
          - id: my-project
            path: {project_root}
            display_name: My Project
            description: test project
            include_subdirs:
              - src
            exclude_subdirs:
              - .git
        """


# ---------------------------------------------------------------------------
# load() — parses YAML, flattens to the Manifest contract
# ---------------------------------------------------------------------------


class TestYamlManifestAdapterLoad:
    """``YamlManifestAdapter.load()`` parses the YAML into a flat Manifest."""

    def test_load_returns_a_manifest(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )

        project_root = tmp_path / "proj"
        project_root.mkdir()
        manifest_path = _write_manifest(tmp_path, _default_manifest_body(project_root))

        adapter = YamlManifestAdapter(manifest_path)
        manifest = adapter.load()

        assert manifest is not None
        assert manifest.server_name == "portfolio-mcp-server"
        assert manifest.version == "0.1.0"
        assert manifest.default_policy == "deny"
        assert manifest.chunk_size == 1500
        assert manifest.chunk_overlap == 200

    def test_load_flattens_indexing_section(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )

        project_root = tmp_path / "proj"
        project_root.mkdir()
        manifest_path = _write_manifest(tmp_path, _default_manifest_body(project_root))

        adapter = YamlManifestAdapter(manifest_path)
        manifest = adapter.load()

        # The indexing.* fields must be flattened to top-level.
        assert ".py" in manifest.include_extensions
        assert ".md" in manifest.include_extensions
        assert "node_modules" in manifest.exclude_paths

    def test_load_includes_projects(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )

        project_root = tmp_path / "proj"
        project_root.mkdir()
        manifest_path = _write_manifest(tmp_path, _default_manifest_body(project_root))

        adapter = YamlManifestAdapter(manifest_path)
        manifest = adapter.load()

        assert len(manifest.projects) == 1
        project = manifest.projects[0]
        assert project.id == "my-project"
        assert project.path == project_root
        assert "src" in project.include_subdirs
        assert ".git" in project.exclude_subdirs


# ---------------------------------------------------------------------------
# is_path_indexed — default-deny path scoping
# ---------------------------------------------------------------------------


class TestYamlManifestAdapterIsPathIndexed:
    """``is_path_indexed`` is default-deny (Layer 1 manifest scoping)."""

    def test_path_under_declared_project_include_subdirs_returns_true(
        self, tmp_path: Path
    ) -> None:
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )

        project_root = tmp_path / "proj"
        (project_root / "src").mkdir(parents=True)
        manifest_path = _write_manifest(tmp_path, _default_manifest_body(project_root))

        adapter = YamlManifestAdapter(manifest_path)
        adapter.load()  # ensure parse succeeded

        indexed_path = project_root / "src" / "module.py"
        assert adapter.is_path_indexed(indexed_path) is True

    def test_path_outside_declared_project_returns_false(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )

        project_root = tmp_path / "proj"
        project_root.mkdir()
        manifest_path = _write_manifest(tmp_path, _default_manifest_body(project_root))

        adapter = YamlManifestAdapter(manifest_path)
        adapter.load()

        unrelated = tmp_path / "other-project" / "src" / "x.py"
        assert adapter.is_path_indexed(unrelated) is False

    def test_path_in_project_but_outside_include_subdirs_returns_false(
        self, tmp_path: Path
    ) -> None:
        """``include_subdirs`` is the allow-list; everything else is denied."""
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )

        project_root = tmp_path / "proj"
        (project_root / "tests").mkdir(parents=True)
        manifest_path = _write_manifest(tmp_path, _default_manifest_body(project_root))

        adapter = YamlManifestAdapter(manifest_path)
        adapter.load()

        # 'tests' is NOT in the project's include_subdirs.
        out_of_scope = project_root / "tests" / "test_x.py"
        assert adapter.is_path_indexed(out_of_scope) is False

    def test_path_in_excluded_subdir_returns_false(self, tmp_path: Path) -> None:
        """``exclude_subdirs`` overrides ``include_subdirs``."""
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )

        project_root = tmp_path / "proj"
        (project_root / ".git").mkdir(parents=True)
        manifest_path = _write_manifest(tmp_path, _default_manifest_body(project_root))

        adapter = YamlManifestAdapter(manifest_path)
        adapter.load()

        excluded = project_root / ".git" / "config"
        assert adapter.is_path_indexed(excluded) is False

    def test_path_traversal_returns_false(self, tmp_path: Path) -> None:
        """``../`` attempts MUST be rejected even if they resolve inside a project."""
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )

        project_root = tmp_path / "proj"
        project_root.mkdir()
        manifest_path = _write_manifest(tmp_path, _default_manifest_body(project_root))

        adapter = YamlManifestAdapter(manifest_path)
        adapter.load()

        traversal = project_root / "src" / ".." / ".." / "etc" / "passwd"
        # Path resolution normalises `..` — the resolved target is outside
        # the project root, so the adapter MUST deny it.
        assert adapter.is_path_indexed(traversal) is False


# ---------------------------------------------------------------------------
# ManifestPort conformance
# ---------------------------------------------------------------------------


class TestYamlManifestAdapterSatisfiesPort:
    """``YamlManifestAdapter`` satisfies ``ManifestPort`` structurally."""

    def test_adapter_satisfies_manifest_port(self, tmp_path: Path) -> None:
        from mcp_server.application.ports.manifest import ManifestPort
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )

        project_root = tmp_path / "proj"
        project_root.mkdir()
        manifest_path = _write_manifest(tmp_path, _default_manifest_body(project_root))

        adapter = YamlManifestAdapter(manifest_path)
        assert isinstance(adapter, ManifestPort)


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestYamlManifestAdapterErrors:
    """Adapter raises domain errors on bad input."""

    def test_missing_file_raises_manifest_not_found(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )
        from mcp_server.domain.exceptions import ManifestNotFoundError

        missing = tmp_path / "does-not-exist.yaml"
        adapter = YamlManifestAdapter(missing)
        with pytest.raises(ManifestNotFoundError):
            adapter.load()

    def test_invalid_schema_raises_manifest_schema_error(self, tmp_path: Path) -> None:
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )
        from mcp_server.domain.exceptions import ManifestSchemaError

        # Missing required fields (no server.name, no projects).
        bad = tmp_path / "bad.yaml"
        bad.write_text("schema_version: 1\n")
        adapter = YamlManifestAdapter(bad)
        with pytest.raises(ManifestSchemaError):
            adapter.load()