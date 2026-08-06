"""Integration tests for the composition root — PR2 + 002-mcp-tools PR1.

After 002-mcp-tools PR1, ``create_composition()`` MUST additionally
wire the two read-only MCP tool use cases:

* ``list_projects_use_case`` — :class:`ListProjectsUseCase`
* ``search_use_case`` — :class:`SearchCodeUseCase`

These were ``None`` placeholders in 001-bootstrap. The remaining four
MCP tool use cases (``explain_architecture``, ``summarize_readme``,
``get_architecture_diagram``, ``ask_portfolio``) and the Pydantic AI
``Agent`` stay ``None`` until 002-mcp-tools PR2 / PR3 land.

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


class TestCompositionWiredAdaptersContract:
    """After PR3 the previously-``None`` adapter fields are real adapters.

    The two future-MCP-tool fields stay ``None`` until 002-mcp-tools.
    """

    def test_embedding_is_real(self) -> None:
        comp = create_composition(AppConfig())
        # Without an API key in tests, the composition falls back to the
        # mock adapter. Either way the field is NOT ``None``.
        assert comp.embedding is not None

    def test_vector_store_is_real(self) -> None:
        from mcp_server.infrastructure.adapters.sqlite_vec_store import SqliteVecStore

        comp = create_composition(AppConfig())
        assert isinstance(comp.vector_store, SqliteVecStore)

    def test_llm_is_real(self) -> None:
        comp = create_composition(AppConfig())
        assert comp.llm is not None

    def test_preindex_use_case_is_real(self) -> None:
        comp = create_composition(AppConfig())
        assert comp.preindex_use_case is not None

    def test_search_use_case_is_real(self) -> None:
        from mcp_server.application.use_cases.search_code import SearchCodeUseCase

        comp = create_composition(AppConfig())
        # 002-mcp-tools PR1: real instance, no longer a ``None`` placeholder.
        assert isinstance(comp.search_use_case, SearchCodeUseCase)

    def test_list_projects_use_case_is_real(self) -> None:
        from mcp_server.application.use_cases.list_projects import ListProjectsUseCase

        comp = create_composition(AppConfig())
        # 002-mcp-tools PR1: real instance, no longer a ``None`` placeholder.
        assert isinstance(comp.list_projects_use_case, ListProjectsUseCase)

    def test_ask_portfolio_use_case_is_real(self) -> None:
        """002-mcp-tools PR3: ``ask_portfolio_use_case`` MUST be wired.

        The Pydantic AI agent is built inside ``create_composition`` and
        injected into the ``AskPortfolioUseCase``. Without this wiring
        the ``ask_portfolio`` MCP tool would fall through to a
        not-wired ``RuntimeError``.
        """
        from mcp_server.application.use_cases.ask_portfolio import AskPortfolioUseCase

        comp = create_composition(AppConfig())
        assert isinstance(comp.ask_portfolio_use_case, AskPortfolioUseCase)

    def test_ask_portfolio_use_case_receives_rate_limiter(self) -> None:
        """Layer 5 application-layer rate limiter is wired into the use case.

        The use case MUST call ``rate_limiter.check(client_ip)`` on
        every invocation; the wiring is the composition root's
        responsibility (ADR-001).
        """
        from mcp_server.application.ports.rate_limiter import RateLimiterPort

        comp = create_composition(AppConfig())
        assert isinstance(comp.ask_portfolio_use_case.rate_limiter, RateLimiterPort)
        assert comp.ask_portfolio_use_case.rate_limiter is comp.rate_limiter


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


class TestCompositionWiresSanitizerWithAudit:
    """Composition injects the audit logger into the sanitizer.

    This is the wiring the verify report flagged as missing: an
    ``output.redacted`` event MUST be emitted whenever the sanitizer
    redacts content. The composition root is the ONLY place where the
    audit logger is shared with the sanitizer, so this test
    complements the unit-level emission test in
    ``tests/unit/security/test_output_sanitizer.py``.
    """

    def test_sanitizer_receives_audit_logger(self) -> None:
        from mcp_server.security.audit import AuditLogger

        comp = create_composition(AppConfig())
        assert isinstance(comp.sanitizer._audit, AuditLogger)
        assert comp.sanitizer._audit is comp.audit

    def test_sanitizer_via_composition_emits_output_redacted(self, capsys) -> None:
        """End-to-end audit emission: composition → sanitizer → audit."""
        import json

        comp = create_composition(AppConfig())
        comp.sanitizer.sanitize(
            "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", source="tool-test"
        )

        out, _ = capsys.readouterr()
        records = [
            json.loads(line)
            for line in out.splitlines()
            if line.strip()
        ]
        # Find the output.redacted event among the captured events.
        redacted = [r for r in records if r.get("event") == "output.redacted"]
        assert len(redacted) == 1
        assert redacted[0]["source"] == "tool-test"
        assert "aws" in redacted[0]["patterns"]
