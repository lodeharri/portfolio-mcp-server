"""Integration tests for Layer 1 — manifest-scoped indexing.

The :class:`YamlManifestAdapter.is_path_indexed` is the SINGLE source of
truth for which filesystem paths leave the index (Layer 1 of the
5-layer security model). This integration test exercises the full
path:

1. Read the real ``config/projects.manifest.yaml`` via the composition
   root.
2. Verify declared projects + their include/exclude subdirs.
3. Verify path-traversal attempts are denied.
4. Verify paths outside any declared project are denied.

Why integration vs. unit? The unit tests in
``tests/unit/infrastructure/adapters/test_yaml_manifest.py`` exercise
the adapter in isolation with a synthetic manifest. This file exercises
the adapter against the real ``projects.manifest.yaml`` shipped with
the repo, so a regression in the actual manifest config is caught.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.composition import create_composition
from mcp_server.config import AppConfig
from mcp_server.infrastructure.adapters.yaml_manifest import YamlManifestAdapter


class TestManifestScopedIndexingIntegration:
    """Layer 1 path scoping against the real ``projects.manifest.yaml``."""

    @pytest.fixture
    def manifest_adapter(self) -> YamlManifestAdapter:
        comp = create_composition(AppConfig())
        adapter = comp.manifest
        assert isinstance(adapter, YamlManifestAdapter)
        # Force load so any schema error surfaces here, not later.
        adapter.load()
        return adapter

    def test_real_manifest_has_two_projects(
        self, manifest_adapter: YamlManifestAdapter
    ) -> None:
        manifest = manifest_adapter.load()
        # The shipped manifest declares finance-coach-latam + landing-page-portfolio.
        ids = {p.id for p in manifest.projects}
        assert "finance-coach-latam" in ids
        assert "landing-page-portfolio" in ids

    def test_real_manifest_includes_default_extensions(
        self, manifest_adapter: YamlManifestAdapter
    ) -> None:
        manifest = manifest_adapter.load()
        # Default include extensions per the shipped manifest.
        assert ".py" in manifest.include_extensions
        assert ".md" in manifest.include_extensions
        assert ".ts" in manifest.include_extensions

    def test_declared_project_path_is_indexed(
        self, manifest_adapter: YamlManifestAdapter
    ) -> None:
        manifest = manifest_adapter.load()
        first_project = manifest.projects[0]
        # A file under the first project's include_subdirs MUST be indexed.
        if first_project.include_subdirs:
            first_subdir = first_project.include_subdirs[0]
            target = first_project.path / first_subdir / "module.py"
            assert manifest_adapter.is_path_indexed(target) is True

    def test_unrelated_path_is_not_indexed(
        self, manifest_adapter: YamlManifestAdapter
    ) -> None:
        # A path clearly outside any declared project is denied.
        unrelated = Path("/tmp/some-other-project/src/main.py")
        assert manifest_adapter.is_path_indexed(unrelated) is False

    def test_path_traversal_is_not_indexed(
        self, manifest_adapter: YamlManifestAdapter
    ) -> None:
        manifest = manifest_adapter.load()
        first_project = manifest.projects[0]
        # `../etc/passwd` resolves outside the project root → denied.
        if first_project.include_subdirs:
            traversal = (
                first_project.path
                / first_project.include_subdirs[0]
                / ".."
                / ".."
                / "etc"
                / "passwd"
            )
            assert manifest_adapter.is_path_indexed(traversal) is False

    def test_excluded_subdir_is_not_indexed(
        self, manifest_adapter: YamlManifestAdapter
    ) -> None:
        manifest = manifest_adapter.load()
        for project in manifest.projects:
            if not project.exclude_subdirs:
                continue
            excluded = project.path / project.exclude_subdirs[0] / "config"
            # The excluded subdir MUST be denied even though the project
            # root is declared.
            assert manifest_adapter.is_path_indexed(excluded) is False