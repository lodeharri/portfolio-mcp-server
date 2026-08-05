"""Tests for ``create_app()`` — the FastAPI factory.

These tests are RED until ``src/mcp_server/app.py`` is implemented.
"""

from __future__ import annotations

from fastapi import FastAPI

from mcp_server.app import create_app
from mcp_server.composition import Composition


class TestCreateApp:
    """``create_app()`` returns a configured FastAPI instance."""

    def test_returns_fastapi_instance(self) -> None:
        app = create_app()
        assert isinstance(app, FastAPI)

    def test_title_is_mcp_server_playground(self) -> None:
        app = create_app()
        assert app.title == "mcp-server-playground"

    def test_composition_attached_to_app_state(self) -> None:
        app = create_app()
        assert hasattr(app.state, "composition")
        assert isinstance(app.state.composition, Composition)

    def test_composition_uses_a_valid_config(self) -> None:
        from mcp_server.config import AppConfig

        app = create_app()
        assert isinstance(app.state.composition.config, AppConfig)

    def test_create_app_is_idempotent(self) -> None:
        """Two calls produce two independently usable FastAPI instances."""
        app1 = create_app()
        app2 = create_app()
        assert app1 is not app2
        assert isinstance(app1, FastAPI)
        assert isinstance(app2, FastAPI)

    def test_two_calls_produce_two_compositions(self) -> None:
        app1 = create_app()
        app2 = create_app()
        assert app1.state.composition is not app2.state.composition
