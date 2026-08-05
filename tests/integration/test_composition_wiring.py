"""Integration tests for the composition root — PR2 wiring contract.

After PR2, ``create_composition()`` MUST return a
:class:`mcp_server.composition.Composition` whose five security adapter
fields are real instances (not ``None`` placeholders):

* ``manifest`` — :class:`YamlManifestAdapter`
* ``secret_scanner`` — :class:`GitleaksScanner`
* ``sanitizer`` — :class:`OutputSanitizer`
* ``rate_limiter`` — :class:`SlowapiRateLimiter`
* ``audit`` — :class:`AuditLogger`

The three preindex-related fields (``embedding``, ``vector_store``,
``preindex_use_case``) and the two future MCP-tool fields
(``search_use_case``, ``list_projects_use_case``) remain ``None`` until
PR3 / 002-mcp-tools land.

The hexagonal invariant test (``test_hexagonal_invariants.py``) ALSO
asserts ``composition.py`` is the only module importing both adapters
and use cases. This file is the focused integration test that verifies
the wiring at runtime, complementing the static AST analysis.
"""

from __future__ import annotations

import pytest

from mcp_server.composition import Composition, create_composition
from mcp_server.config import AppConfig
from mcp_server.infrastructure.adapters.yaml_manifest import YamlManifestAdapter
from mcp_server.security.audit import AuditLogger
from mcp_server.security.gitleaks_scanner import GitleaksScanner
from mcp_server.security.output_sanitizer import OutputSanitizer
from mcp_server.security.rate_limiter import SlowapiRateLimiter


class TestCompositionWiringContract:
    """The composition root wires the five PR2 security adapters."""

    def test_returns_a_composition(self) -> None:
        comp = create_composition(AppConfig())
        assert isinstance(comp, Composition)

    def test_manifest_adapter_is_real(self) -> None:
        comp = create_composition(AppConfig())
        assert isinstance(comp.manifest, YamlManifestAdapter)

    def test_secret_scanner_is_real(self) -> None:
        comp = create_composition(AppConfig())
        assert isinstance(comp.secret_scanner, GitleaksScanner)

    def test_sanitizer_is_real(self) -> None:
        comp = create_composition(AppConfig())
        assert isinstance(comp.sanitizer, OutputSanitizer)

    def test_rate_limiter_is_real(self) -> None:
        comp = create_composition(AppConfig())
        assert isinstance(comp.rate_limiter, SlowapiRateLimiter)

    def test_audit_logger_is_real(self) -> None:
        comp = create_composition(AppConfig())
        assert isinstance(comp.audit, AuditLogger)


class TestCompositionPlaceholderContract:
    """The preindex-related and future MCP-tool fields remain ``None``."""

    def test_embedding_is_none(self) -> None:
        comp = create_composition(AppConfig())
        assert comp.embedding is None

    def test_vector_store_is_none(self) -> None:
        comp = create_composition(AppConfig())
        assert comp.vector_store is None

    def test_llm_is_none(self) -> None:
        comp = create_composition(AppConfig())
        assert comp.llm is None

    def test_preindex_use_case_is_none(self) -> None:
        comp = create_composition(AppConfig())
        assert comp.preindex_use_case is None

    def test_search_use_case_is_none(self) -> None:
        comp = create_composition(AppConfig())
        assert comp.search_use_case is None

    def test_list_projects_use_case_is_none(self) -> None:
        comp = create_composition(AppConfig())
        assert comp.list_projects_use_case is None


class TestCompositionIsFrozen:
    """The composition root is frozen (no mid-request mutation)."""

    def test_frozen_assignment_raises(self) -> None:
        import dataclasses

        comp = create_composition(AppConfig())
        with pytest.raises(dataclasses.FrozenInstanceError):
            comp.manifest = None  # type: ignore[misc]


class TestCompositionAuditSharedAcrossAdapters:
    """The audit logger is shared with the scanner and rate limiter."""

    def test_gitleaks_scanner_receives_audit(self) -> None:
        from mcp_server.security.gitleaks_scanner import GitleaksScanner

        comp = create_composition(AppConfig())
        # The scanner was constructed with the same audit instance.
        assert isinstance(comp.secret_scanner, GitleaksScanner)
        assert comp.secret_scanner._audit is comp.audit

    def test_rate_limiter_receives_audit(self) -> None:
        comp = create_composition(AppConfig())
        assert isinstance(comp.rate_limiter, SlowapiRateLimiter)
        assert comp.rate_limiter._audit is comp.audit


class TestCompositionManifestLoadedEndToEnd:
    """The manifest adapter can load the project's real manifest end-to-end."""

    def test_load_returns_a_manifest(self) -> None:
        comp = create_composition(AppConfig())
        manifest = comp.manifest.load()
        assert manifest.server_name == "portfolio-mcp-server"
        assert len(manifest.projects) >= 1

    def test_is_path_indexed_works_after_load(self) -> None:
        from pathlib import Path

        comp = create_composition(AppConfig())
        # Force the manifest to load (lazy).
        comp.manifest.load()
        # A path clearly outside any declared project returns False.
        unrelated = Path("/tmp/this-path-does-not-exist-anywhere.py")  # noqa: S108
        assert comp.manifest.is_path_indexed(unrelated) is False


class TestCompositionFailsFastOnMissingManifest:
    """``create_composition`` propagates manifest errors at startup.

    Pre-PR2 fix: the composition root constructed ``YamlManifestAdapter``
    lazily and never called ``load()``. A missing manifest returned a
    :class:`Composition` whose ``manifest`` was an unloaded adapter; the
    failure surfaced only when the first request walked the index. The
    spec requires fail-fast on startup so the preindex pipeline aborts
    with a non-zero exit code (task 2.14 + ADR-001 eager wiring).
    """

    def test_missing_manifest_path_raises_manifest_not_found(self, tmp_path) -> None:
        from mcp_server.domain.exceptions import ManifestNotFoundError

        missing = tmp_path / "no-such-manifest.yaml"
        config = AppConfig(manifest_path=missing)
        with pytest.raises(ManifestNotFoundError):
            create_composition(config)

    def test_invalid_manifest_schema_raises_manifest_schema_error(self, tmp_path) -> None:
        from mcp_server.domain.exceptions import ManifestSchemaError

        bad = tmp_path / "bad.yaml"
        bad.write_text("schema_version: 1\n")
        config = AppConfig(manifest_path=bad)
        with pytest.raises(ManifestSchemaError):
            create_composition(config)

    def test_manifest_with_no_projects_raises_manifest_schema_error(self, tmp_path) -> None:
        """A manifest missing ``projects`` MUST fail at startup.

        Layer 1 default-deny requires at least one declared project. If
        ``create_composition`` accepted such a manifest, the preindex
        pipeline would boot with no scope and silently index nothing.
        """
        from mcp_server.domain.exceptions import ManifestSchemaError

        manifest_path = tmp_path / "no-projects.yaml"
        manifest_path.write_text(
            "schema_version: 1\n"
            "server:\n"
            "  name: portfolio-mcp-server\n"
            "  version: 0.1.0\n"
            "  description: test\n"
            "indexing:\n"
            "  default_policy: deny\n"
            "  chunk_size: 1500\n"
            "  chunk_overlap: 200\n"
            "# no projects:\n"
        )
        config = AppConfig(manifest_path=manifest_path)
        with pytest.raises(ManifestSchemaError):
            create_composition(config)
