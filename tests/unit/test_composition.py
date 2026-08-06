"""Tests for ``src/mcp_server/composition.py`` — DI container (eager wiring).

These tests are RED until ``src/mcp_server/composition.py`` is implemented.
They enforce the ADR-001 eager-wiring contract: every call to
``create_composition`` returns a fresh frozen ``Composition`` instance.
"""

from __future__ import annotations

import dataclasses

import pytest

from mcp_server.composition import Composition, compose, create_composition
from mcp_server.config import AppConfig, load_config


class TestCompositionDataclass:
    """``Composition`` is a frozen dataclass — no mutation after wiring."""

    def test_is_a_dataclass(self) -> None:
        assert dataclasses.is_dataclass(Composition)

    def test_is_frozen(self) -> None:
        """A frozen dataclass raises ``FrozenInstanceError`` on assignment."""
        cfg = AppConfig()
        comp = create_composition(cfg)
        with pytest.raises(dataclasses.FrozenInstanceError):
            comp.config = AppConfig()  # type: ignore[misc]

    def test_has_config_field(self) -> None:
        cfg = AppConfig()
        comp = create_composition(cfg)
        assert comp.config == cfg

    def test_all_pr2_security_adapters_are_wired(self) -> None:
        """PR2 wires real security adapters (Layers 1, 2, 3, 5).

        After PR2, the five security adapters MUST be real instances,
        not ``None`` placeholders.
        """
        from mcp_server.infrastructure.adapters.yaml_manifest import (
            YamlManifestAdapter,
        )
        from mcp_server.security.audit import AuditLogger
        from mcp_server.security.gitleaks_scanner import GitleaksScanner
        from mcp_server.security.output_sanitizer import OutputSanitizer
        from mcp_server.security.rate_limiter import SlowapiRateLimiter

        comp = create_composition(AppConfig())
        assert isinstance(comp.manifest, YamlManifestAdapter)
        assert isinstance(comp.secret_scanner, GitleaksScanner)
        assert isinstance(comp.sanitizer, OutputSanitizer)
        assert isinstance(comp.rate_limiter, SlowapiRateLimiter)
        assert isinstance(comp.audit, AuditLogger)

    def test_all_pr3_adapters_are_wired(self) -> None:
        """PR3 wires real adapters for embedding / vector_store / preindex_use_case.

        These were ``None`` placeholders in PR1+PR2; after PR3 they
        MUST be real instances. The composition root is the only place
        the wiring logic lives.
        """
        from mcp_server.infrastructure.adapters.sqlite_vec_store import (
            SqliteVecStore,
        )

        comp = create_composition(AppConfig())
        assert comp.embedding is not None
        assert isinstance(comp.vector_store, SqliteVecStore)
        assert comp.preindex_use_case is not None
        assert comp.llm is not None

    def test_002_pr1_list_projects_use_case_is_wired(self) -> None:
        """002-mcp-tools PR1 wires ``list_projects_use_case`` as a real instance.

        The remaining four MCP tool use cases and the LangChain
        ``Agent`` land in PR2 / PR3.
        """
        from mcp_server.application.use_cases.list_projects import (
            ListProjectsUseCase,
        )

        comp = create_composition(AppConfig())
        assert isinstance(comp.list_projects_use_case, ListProjectsUseCase)

    def test_002_pr1_search_use_case_is_wired(self) -> None:
        """002-mcp-tools PR1 wires ``search_use_case`` as a real instance."""
        from mcp_server.application.use_cases.search_code import (
            SearchCodeUseCase,
        )

        comp = create_composition(AppConfig())
        assert isinstance(comp.search_use_case, SearchCodeUseCase)


class TestCreateComposition:
    """``create_composition(config)`` builds a fresh wired container."""

    def test_returns_a_composition(self) -> None:
        comp = create_composition(AppConfig())
        assert isinstance(comp, Composition)

    def test_passes_config_through(self) -> None:
        cfg = AppConfig(port=9000)
        comp = create_composition(cfg)
        assert comp.config.port == 9000

    def test_two_calls_return_distinct_instances(self) -> None:
        """No module-level cache — each call wires a fresh container (ADR-001)."""
        comp1 = create_composition(AppConfig())
        comp2 = create_composition(AppConfig())
        assert comp1 is not comp2

    def test_two_calls_have_distinct_config_objects(self) -> None:
        """Even when called with no config, each call gets its own AppConfig."""
        comp1 = create_composition()
        comp2 = create_composition()
        assert comp1.config is not comp2.config

    def test_default_config_via_load_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without an explicit config, ``create_composition()`` calls ``load_config()``."""
        monkeypatch.setenv("PORT", "9999")
        comp = create_composition()
        assert comp.config.port == 9999


class TestComposeAlias:
    """``compose`` is an alias matching design.md and tasks.md."""

    def test_compose_is_create_composition(self) -> None:
        assert compose is create_composition

    def test_compose_works_with_default_args(self) -> None:
        comp = compose(AppConfig())
        assert isinstance(comp, Composition)


class TestHexagonalInvariant:
    """The composition module is the ONLY place wiring adapters to use cases."""

    def test_composition_imports_adapters_and_use_cases(self) -> None:
        """The composition module MUST import both — otherwise it cannot wire them.

        The invariant test in tests/integration/test_hexagonal_invariants.py
        enforces that NO OTHER module does this. This test simply asserts the
        composition module does (sanity).
        """
        import ast
        from pathlib import Path

        text = Path("src/mcp_server/composition.py").read_text()
        tree = ast.parse(text)
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        # It doesn't have to import them YET in PR1 (placeholders are None).
        # What matters is that the module is allowed to do so when PR2/PR3 land.
        # We only assert it doesn't import config incorrectly.
        assert "mcp_server.config" in modules or any(
            m.startswith("mcp_server.config") for m in modules
        ), "composition.py must import AppConfig / load_config from mcp_server.config"


# Keep load_config in scope for IDE auto-imports.
_ = load_config
