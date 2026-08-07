"""Tests for the rendered Jinja2 templates (5 pages in the playground).

These tests pin user-facing copy that's meaningful to the recruiter /
HR audience — footer removal (no tech-stack worship), Spanish
translations of every visible string, the chat composer bottom margin
visual fix, and the clear-history button. Every assertion runs against
the rendered HTML of the page (through ``create_app()`` / the standalone
chat router) so any regressions in the template files or the CSS smoke
test will trip.

Per change 007-playground-ui-polish tasks 1, 2, 3, 4.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PLAYGROUND_TEMPLATES_DIR = (
    Path(__file__).resolve().parents[5] / "playground" / "templates"
)
PLAYGROUND_STATIC_DIR = (
    Path(__file__).resolve().parents[5] / "playground" / "static"
)


# ---------------------------------------------------------------------------
# Page-rendering helpers + fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def web_app():
    """Build the FastAPI app once per module — mounts the web router."""
    from mcp_server.app import create_app
    from mcp_server.config import AppConfig

    return create_app(AppConfig(gemini_api_key=""))


@pytest.fixture(scope="module")
def web_client(web_app):
    """A TestClient that has entered the FastAPI lifespan context."""
    from fastapi.testclient import TestClient

    with TestClient(web_app) as client:
        yield client


@pytest.fixture(scope="module")
def chat_router():
    """Standalone chat router for the chat.html smoke tests.

    The chat page is mounted via ``build_chat_router()``; the unit tests
    for the SSE pipeline use this fixture, and so do the page-content
    smoke tests in this module.
    """
    from mcp_server.interfaces.http.web.chat import build_chat_router

    return build_chat_router()


# ---------------------------------------------------------------------------
# Task 1 — footer removal (recruiter-facing pages must not advertise tech stack)
# ---------------------------------------------------------------------------


class TestFooterRemoval:
    """The portfolio is shown to HR/recruiters. The "Built with FastAPI
    + HTMX + Jinja2 + the 5-layer security model" footer is exactly the
    tech-stack worship that turns a recruiter off (they don't care what
    the server is written in; they care what the demo does). The
    index.html "MCP transport ... Claude Desktop" footer is also recruiter
    noise — the JSON-RPC transport is for developers, not visitors.

    Both footers MUST be removed; the CSS rule for ``.playground-footer``
    MUST also be removed (no longer used by any template).
    """

    def test_base_template_does_not_render_tech_stack_footer(
        self, web_client: object
    ) -> None:
        """GET / extends base.html; the rendered HTML MUST NOT contain
        the "Built with FastAPI + HTMX + Jinja2 + the 5-layer security model"
        string that lived in the base.html footer.
        """
        text = web_client.get("/").text  # type: ignore[attr-defined]
        assert "Built with FastAPI" not in text, (
            "base.html still renders the tech-stack footer; recruiters "
            "see 'Built with FastAPI + HTMX + Jinja2 + the 5-layer "
            "security model' — remove the entire <footer class='playground-footer'> "
            "block from base.html"
        )
        assert "5-layer security model" not in text, (
            "the '5-layer security model' string is a tech-stack tell "
            "that must not reach the recruiter landing page"
        )

    def test_base_template_does_not_render_any_playground_footer_class(
        self, web_client: object
    ) -> None:
        """The landing page MUST NOT contain any ``class="playground-footer"``
        element. The footer block has been removed from base.html and
        any leftover reference is dead code.
        """
        text = web_client.get("/").text  # type: ignore[attr-defined]
        assert "playground-footer" not in text, (
            "rendered landing page must not contain the .playground-footer class "
            "(the footer block was removed from base.html)"
        )

    def test_index_template_does_not_render_mcp_transport_footer(
        self, web_client: object
    ) -> None:
        """GET / (index.html) MUST NOT contain the "MCP transport ... live at /mcp"
        footer text. That copy advertises the raw JSON-RPC transport to
        recruiters, who can't actually use it from a browser (the
        missing-Accept-header 406 is the whole reason /mcp-ui exists).
        """
        text = web_client.get("/").text  # type: ignore[attr-defined]
        # The "MCP transport" string is the strongest unique marker of
        # the index.html footer block.
        assert "MCP transport" not in text, (
            "index.html still renders the 'MCP transport ... live at /mcp' "
            "footer; remove the entire <footer class='playground-footer'> "
            "block from index.html"
        )
        # Original surrounding copy was "MCP transport (JSON-RPC over
        # Streamable HTTP) is live at /mcp for clients like Claude Desktop,
        # Cursor, or npx @modelcontextprotocol/inspector." — assert the
        # Claude Desktop and inspector copy also disappears.
        assert "Claude Desktop" not in text, (
            "the 'for clients like Claude Desktop' footer copy must be "
            "removed from index.html; it's dev-facing, not recruiter-facing"
        )

    def test_css_no_longer_defines_playground_footer_rule(self) -> None:
        """``playground/static/style.css`` MUST NOT declare a rule for
        ``.playground-footer`` — the class is no longer used by any
        template (task 1 deletes the rule).
        """
        css_path = PLAYGROUND_STATIC_DIR / "style.css"
        assert css_path.is_file(), f"style.css not found at {css_path}"
        css = css_path.read_text(encoding="utf-8")
        pattern = re.escape("footer.playground-footer") + r"\s*\{[^}]+\}"
        assert not re.search(pattern, css), (
            "style.css still defines a 'footer.playground-footer' rule; "
            "delete the block — the class is no longer used by any template"
        )

    def test_css_no_orphan_playground_footer_selector_anywhere(self) -> None:
        """The literal string ``.playground-footer`` MUST NOT appear
        anywhere in style.css (no orphan selector fragments, no
        commented-out rules).
        """
        css_path = PLAYGROUND_STATIC_DIR / "style.css"
        css = css_path.read_text(encoding="utf-8")
        assert ".playground-footer" not in css, (
            "style.css contains an orphan '.playground-footer' selector; "
            "the class is no longer used by any template"
        )
