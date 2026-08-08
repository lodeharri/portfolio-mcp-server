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

PLAYGROUND_TEMPLATES_DIR = Path(__file__).resolve().parents[5] / "playground" / "templates"
PLAYGROUND_STATIC_DIR = Path(__file__).resolve().parents[5] / "playground" / "static"


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


@pytest.fixture
def chat_client_factory(chat_router):
    """Return a factory that builds an AsyncClient wired to the
    standalone chat router.

    The chat router reads ``request.app.state.composition`` to look up
    the use case; the factory wires a minimal stub composition so the
    page renders. Tests then call the factory inside their own
    ``async with`` context so the test function fully controls when the
    client is open / closed.
    """
    from collections.abc import AsyncIterator

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from mcp_server.application.use_cases.ask_portfolio import (
        AskPortfolioChunk,
        AskPortfolioRequest,
        AskPortfolioResult,
    )

    class _StubAskPortfolio:
        async def astream(self, request: AskPortfolioRequest) -> AsyncIterator[AskPortfolioChunk]:
            yield AskPortfolioChunk(
                kind="done",
                result=AskPortfolioResult(answer="ok"),
            )

    class _StubComposition:
        ask_portfolio_use_case = _StubAskPortfolio()
        sanitizer = None

    def _build() -> AsyncClient:
        app = FastAPI()
        app.state.composition = _StubComposition()  # type: ignore[attr-defined]
        app.include_router(chat_router)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")

    return _build


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

    def test_base_template_does_not_render_tech_stack_footer(self, web_client: object) -> None:
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

    def test_index_template_does_not_render_mcp_transport_footer(self, web_client: object) -> None:
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


# ---------------------------------------------------------------------------
# Task 5 — remove /mcp nav link from base.html (browsers hit 406 on raw SSE)
# ---------------------------------------------------------------------------


class TestMcpNavLinkRemoval:
    """``href="/mcp"`` in the top nav resolves to the raw JSON-RPC
    Streamable-HTTP transport endpoint, which requires an
    ``Accept: application/json, text/event-stream`` header that browsers
    normally don't send — the result is a 406 ``Not Acceptable`` error.
    The browser-friendly alternative lives at ``/mcp-ui`` and is already
    in the nav. The raw ``/mcp`` link is recruiter-facing tech debt that
    MUST be removed from the nav.
    """

    def test_nav_does_not_link_to_raw_mcp_endpoint(self, web_client: object) -> None:
        """The rendered nav MUST NOT contain ``href="/mcp"`` (no raw
        JSON-RPC link in the top navigation). The browser-friendly
        ``/mcp-ui`` link MUST still be present.
        """
        text = web_client.get("/").text  # type: ignore[attr-defined]
        # Locate the nav block by isolating between <nav and </nav>.
        nav_match = re.search(r"<nav\b[^>]*>(.*?)</nav>", text, re.DOTALL)
        assert nav_match, "base.html must render a <nav> block"
        nav_body = nav_match.group(1)
        assert 'href="/mcp"' not in nav_body, (
            'the nav still contains a <a href="/mcp">MCP endpoint</a> '
            "link that returns 406 in browsers; remove it (browser "
            "users should use /mcp-ui instead)"
        )
        # Sanity: the browser-friendly alternative is still present.
        assert 'href="/mcp-ui"' in nav_body, (
            "the nav must still link to /mcp-ui (the browser-friendly JSON-RPC UI)"
        )

    def test_nav_block_in_base_template_does_not_mention_mcp_endpoint_string(
        self, web_client: object
    ) -> None:
        """The literal label "MCP endpoint" must be gone from the nav.
        The label was the only English-language nav link; the rest of
        the nav will be translated to Spanish in Task 2.
        """
        text = web_client.get("/").text  # type: ignore[attr-defined]
        nav_match = re.search(r"<nav\b[^>]*>(.*?)</nav>", text, re.DOTALL)
        assert nav_match, "base.html must render a <nav> block"
        nav_body = nav_match.group(1)
        assert "MCP endpoint" not in nav_body, (
            "the nav label 'MCP endpoint' must be removed; that link "
            "takes users to a 404 error in their browser"
        )


# ---------------------------------------------------------------------------
# Task 2 — Spanish translation of all user-facing copy
# ---------------------------------------------------------------------------


class TestBaseTemplateSpanishTranslation:
    """The base.html chrome (nav + lang attribute) MUST be in Spanish.

    The landing page (``GET /``) extends base.html, so the rendered HTML
    is the union of base.html and index.html. We assert the Spanish
    nav labels on the landing render.
    """

    def test_html_lang_attribute_is_spanish(self, web_client: object) -> None:
        """``<html lang="...">`` MUST be ``"es"`` so screen readers and
        browser translation pick Spanish.
        """
        text = web_client.get("/").text  # type: ignore[attr-defined]
        assert re.search(r'<html\s+lang="es"', text), (
            'base.html must declare <html lang="es">; the portfolio '
            "is targeted at Spanish-speaking recruiters"
        )

    def test_home_nav_link_label_is_inicio(self, web_client: object) -> None:
        """The Home nav link MUST read "Inicio" in Spanish."""
        text = web_client.get("/").text  # type: ignore[attr-defined]
        nav_match = re.search(r"<nav\b[^>]*>(.*?)</nav>", text, re.DOTALL)
        assert nav_match, "base.html must render a <nav> block"
        nav_body = nav_match.group(1)
        assert ">Inicio<" in nav_body, "the Home nav link must read 'Inicio' (Spanish for Home)"

    def test_playground_nav_link_removed(self, web_client: object) -> None:
        """The /playground nav link MUST be gone — /mcp-ui is the sole
        browser-facing tool surface (Phase 2 cleanup).
        """
        text = web_client.get("/").text  # type: ignore[attr-defined]
        nav_match = re.search(r"<nav\b[^>]*>(.*?)</nav>", text, re.DOTALL)
        nav_body = nav_match.group(1)
        assert 'href="/playground"' not in nav_body, (
            "the nav still links to /playground — Phase 2 removes the "
            "parallel hand-crafted surface; /mcp-ui is the only tool link"
        )

    def test_mcp_browser_nav_link_kept(self, web_client: object) -> None:
        """The MCP browser nav link MUST still point at /mcp-ui."""
        text = web_client.get("/").text  # type: ignore[attr-defined]
        nav_match = re.search(r"<nav\b[^>]*>(.*?)</nav>", text, re.DOTALL)
        nav_body = nav_match.group(1)
        assert 'href="/mcp-ui"' in nav_body, (
            "the nav must still link to /mcp-ui (the sole browser tool surface)"
        )

    def test_mcp_browser_nav_link_label_is_explorador_mcp(self, web_client: object) -> None:
        """The MCP browser nav link MUST read "Explorador MCP" in Spanish."""
        text = web_client.get("/").text  # type: ignore[attr-defined]
        nav_match = re.search(r"<nav\b[^>]*>(.*?)</nav>", text, re.DOTALL)
        nav_body = nav_match.group(1)
        assert "Explorador MCP" in nav_body, (
            "the MCP browser nav link must read 'Explorador MCP' (Spanish for 'MCP explorer')"
        )

    def test_sidebar_project_name_kept_as_proper_noun(self, web_client: object) -> None:
        """The sidebar project name ``portfolio-mcp-server`` is a proper
        noun (the project name) and MUST stay in English.
        """
        text = web_client.get("/").text  # type: ignore[attr-defined]
        assert "portfolio-mcp-server" in text, (
            "the sidebar project name 'portfolio-mcp-server' is a proper "
            "noun and must be kept verbatim"
        )


class TestIndexPageSpanishTranslation:
    """The landing page (index.html) MUST be in Spanish.

    Translated strings check (selected):
    * Page title: "Harrison Rodriguez — Servidor MCP de Portfolio"
    * Section heading: "Proyectos indexados"
    * Per-project chunk count: "chunks indexados"
    * Per-project link: "abrir en el explorador" (points at /mcp-ui)
    * Empty state: "Aún no hay proyectos en el manifiesto."
    * Primary CTA: "Explorador MCP"
    * Secondary CTA: "Chatear con el agente del portfolio"
    """

    def test_page_title_is_spanish(self, web_client: object) -> None:
        text = web_client.get("/").text  # type: ignore[attr-defined]
        assert "Harrison Rodriguez" in text
        assert "Servidor MCP de Portfolio" in text, (
            "the index.html title must read 'Harrison Rodriguez — "
            "Servidor MCP de Portfolio' in Spanish"
        )

    def test_indexed_projects_section_heading_is_spanish(self, web_client: object) -> None:
        """The 'Indexed projects' h2 MUST be translated to 'Proyectos indexados'."""
        text = web_client.get("/").text  # type: ignore[attr-defined]
        assert "Proyectos indexados" in text, (
            "index.html section heading must read 'Proyectos indexados' (Spanish)"
        )
        assert "Indexed projects" not in text, (
            "the English 'Indexed projects' heading must be removed"
        )

    def test_per_project_chunk_count_label_is_spanish(self, web_client: object) -> None:
        """The per-project chunk count label MUST read 'chunks indexados'."""
        text = web_client.get("/").text  # type: ignore[attr-defined]
        assert "chunks indexados" in text, (
            "the per-project chunk count label must read 'chunks indexados' (Spanish)"
        )
        assert "indexed chunks" not in text, "the English 'indexed chunks' label must be removed"

    def test_per_project_open_in_explorer_link_is_spanish(self, web_client: object) -> None:
        """The per-project 'open in explorer' link MUST point at /mcp-ui
        and MUST be in Spanish (Phase 2 dropped the /playground link).
        """
        text = web_client.get("/").text  # type: ignore[attr-defined]
        assert "abrir en el explorador" in text, (
            "the per-project link must read 'abrir en el explorador' (Spanish)"
        )
        assert "open in playground" not in text, (
            "the English 'open in playground' link label must be removed"
        )
        assert 'href="/playground"' not in text, (
            "the per-project link must not point at the removed /playground surface"
        )

    def test_empty_state_message_is_spanish(self, web_client: object) -> None:
        """The 'No projects declared in the manifest yet.' empty-state
        message MUST be translated to 'Aún no hay proyectos en el manifesto.'.
        """
        template_text = PLAYGROUND_TEMPLATES_DIR.joinpath("index.html").read_text(encoding="utf-8")
        # Template-level check (the empty branch is rendered only when
        # the manifest is empty; the renderer may not exercise it in
        # this fixture).
        assert "No projects declared in the manifest yet." not in template_text, (
            "the English empty-state message must be removed from index.html"
        )
        assert "Aún no hay proyectos en el manifiesto." in template_text, (
            "index.html must declare the Spanish empty-state message "
            "'Aún no hay proyectos en el manifiesto.'"
        )

    def test_primary_cta_label_is_spanish(self, web_client: object) -> None:
        """The primary CTA MUST read 'Explorador MCP' and MUST point at /mcp-ui.

        Phase 2 dropped the 'Probar el playground MCP' CTA in favor of
        a direct 'Explorador MCP' link to /mcp-ui — the sole browser
        tool surface.
        """
        text = web_client.get("/").text  # type: ignore[attr-defined]
        assert "Explorador MCP" in text, (
            "the primary CTA must read 'Explorador MCP' (Spanish)"
        )
        assert "Probar el playground MCP" not in text, (
            "the legacy 'Probar el playground MCP' CTA must be removed"
        )
        assert 'href="/playground"' not in text, (
            "the primary CTA must not point at the removed /playground surface"
        )

    def test_secondary_cta_label_is_spanish(self, web_client: object) -> None:
        """The secondary CTA MUST read 'Chatear con el agente del portfolio'."""
        text = web_client.get("/").text  # type: ignore[attr-defined]
        assert "Chatear con el agente del portfolio" in text, (
            "the secondary CTA must read 'Chatear con el agente del portfolio' (Spanish)"
        )
        assert "Chat with the portfolio agent" not in text, (
            "the English 'Chat with the portfolio agent' CTA must be removed"
        )


class TestChatPageSpanishTranslation:
    """GET /chat page MUST be in Spanish.

    Translated strings check (selected):
    * Page title: "Chat — Playground MCP"
    * h1: "Chatea con el agente"
    * Status label: "Listo"
    * Role labels: "Tú", "Agente", "Herramienta"
    * Send button: "Enviar"
    * Meta hint: "Enter para enviar · Shift+Enter para nueva línea"
    * Connection lost message: "Conexión perdida."
    * Retry button: "¿Reintentar?"
    * JS-required status: "Se requiere JavaScript"
    """

    @pytest.mark.asyncio
    async def test_page_title_is_spanish(self, chat_client_factory) -> None:
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        assert "Chat — Playground MCP" in response.text, (
            "chat.html title must read 'Chat — Playground MCP' (Spanish)"
        )

    @pytest.mark.asyncio
    async def test_h1_heading_is_spanish(self, chat_client_factory) -> None:
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        assert "Chatea con el agente" in response.text, (
            "chat.html h1 must read 'Chatea con el agente' (Spanish)"
        )

    @pytest.mark.asyncio
    async def test_status_label_is_ready_in_spanish(self, chat_client_factory) -> None:
        """The visible 'Ready' status label MUST be translated to 'Listo'."""
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        assert "Listo" in response.text, (
            "chat.html default status label must read 'Listo' (Spanish)"
        )

    @pytest.mark.asyncio
    async def test_send_button_label_is_spanish(self, chat_client_factory) -> None:
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        assert ">Enviar<" in response.text, "chat.html submit button must read 'Enviar' (Spanish)"

    @pytest.mark.asyncio
    async def test_input_meta_hint_is_spanish(self, chat_client_factory) -> None:
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        assert "Enter para enviar" in response.text, (
            "chat.html meta hint must include 'Enter para enviar'"
        )
        assert "Shift+Enter para nueva línea" in response.text, (
            "chat.html meta hint must include 'Shift+Enter para nueva línea'"
        )

    @pytest.mark.asyncio
    async def test_connection_lost_message_is_spanish(self, chat_client_factory) -> None:
        """The 'Connection lost.' error message MUST be translated to
        'Conexión perdida.' (the message is embedded in the inline JS
        that handles network failures).
        """
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        assert "Conexión perdida." in response.text, (
            "chat.html inline client must call showInlineRetry with "
            "'Conexión perdida.' (Spanish for 'Connection lost.')"
        )

    @pytest.mark.asyncio
    async def test_retry_button_label_is_spanish(self, chat_client_factory) -> None:
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        assert "¿Reintentar?" in response.text, (
            "chat.html retry button must read '¿Reintentar?' (Spanish)"
        )

    @pytest.mark.asyncio
    async def test_user_and_agent_role_labels_are_spanish(self, chat_client_factory) -> None:
        """The JS-side role labels MUST be translated: 'You' → 'Tú',
        'Agent' → 'Agente'. (These are the meta-row labels rendered by
        renderMessage.)
        """
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        # The renderMessage JS uses ternary on messageRole; the user label
        # is "Tú" (capitalized as proper role label), the agent label
        # is "Agente".
        assert '"Tú"' in response.text or "'Tú'" in response.text, (
            "chat.html renderMessage must label user role as 'Tú' (Spanish)"
        )
        assert '"Agente"' in response.text or "'Agente'" in response.text, (
            "chat.html renderMessage must label agent role as 'Agente' (Spanish)"
        )

    @pytest.mark.asyncio
    async def test_js_required_status_is_spanish(self, chat_client_factory) -> None:
        """The 'JavaScript required' status label MUST be translated to
        'Se requiere JavaScript'. (The JS path detects no fetch /
        ReadableStream and reports it to the recruiter.)
        """
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        assert "Se requiere JavaScript" in response.text, (
            "chat.html no-JS status label must read 'Se requiere JavaScript' "
            "(Spanish for 'JavaScript required')"
        )


class TestMcpBrowserPageSpanishTranslation:
    """GET /mcp-ui page MUST be in Spanish.

    Translated strings check (selected):
    * Page title: "Explorador MCP — Playground JSON-RPC"
    * h1: "Explorador MCP"
    * Quick reference heading: "Referencia rápida"
    * Server / Protocol / Tools labels
    * "No se pudieron enumerar las herramientas:" error message
    * Tool call button: "Llamar {tool}"
    * "Envía el formulario de arriba para llamar esta herramienta." hint
    * "inputSchema en bruto" details summary
    """

    def test_page_title_is_spanish(self, web_client: object) -> None:
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        assert "Explorador MCP" in text, (
            "mcp_browser.html title must include 'Explorador MCP' (Spanish)"
        )
        # The English "MCP Browser" string is allowed inside JS comments
        # (dev-facing reference) per the task translation rules. Assert
        # it does NOT appear in the user-visible <title> tag.
        title_match = re.search(r"<title>(.*?)</title>", text, re.DOTALL)
        assert title_match, "mcp_browser.html must render a <title> tag"
        assert "MCP Browser" not in title_match.group(1), (
            "the user-visible <title> must not contain 'MCP Browser' "
            "(English label); the JS comment is allowed to keep it"
        )

    def test_h1_heading_is_spanish(self, web_client: object) -> None:
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        # The h1 should read "Explorador MCP", not "MCP Browser".
        h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.DOTALL)
        assert h1_match, "mcp_browser.html must render an <h1>"
        assert "Explorador MCP" in h1_match.group(1), (
            "the mcp_browser.html h1 must read 'Explorador MCP' (Spanish)"
        )

    def test_quick_reference_heading_is_spanish(self, web_client: object) -> None:
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        assert "Referencia rápida" in text, (
            "mcp_browser.html quick-reference section must read 'Referencia rápida' (Spanish)"
        )

    def test_server_protocol_tools_labels_are_spanish(self, web_client: object) -> None:
        """Server / Protocol / Tools labels MUST be translated:
        Server → Servidor, Protocol → Protocolo, Tools → Herramientas.
        """
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        assert "Servidor:" in text, "mcp_browser.html Server label must read 'Servidor:' (Spanish)"
        assert "Protocolo:" in text, (
            "mcp_browser.html Protocol label must read 'Protocolo:' (Spanish)"
        )
        assert "Herramientas:" in text, (
            "mcp_browser.html Tools label must read 'Herramientas:' (Spanish)"
        )

    def test_call_tool_button_label_is_spanish(self, web_client: object) -> None:
        """The 'Call {tool}' button label MUST be translated to 'Llamar {tool}'."""
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        assert "Llamar " in text, (
            "mcp_browser.html tool call button must read 'Llamar {tool}' (Spanish)"
        )

    def test_input_schema_summary_is_spanish(self, web_client: object) -> None:
        """The 'raw inputSchema' <summary> MUST be translated to
        'inputSchema en bruto'.
        """
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        assert "inputSchema en bruto" in text, (
            "mcp_browser.html raw inputSchema summary must read 'inputSchema en bruto' (Spanish)"
        )

    def test_placeholder_result_text_is_spanish(self, web_client: object) -> None:
        """The 'Submit a form above to call this tool.' placeholder text
        MUST be translated to 'Envía el formulario de arriba para llamar
        esta herramienta.'.
        """
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        assert "Envía el formulario de arriba para llamar esta herramienta." in text, (
            "mcp_browser.html placeholder result text must read "
            "'Envía el formulario de arriba para llamar esta herramienta.' (Spanish)"
        )

    def test_enumerate_tools_error_message_is_spanish(self, web_client: object) -> None:
        """The 'Could not enumerate tools:' error message MUST be translated
        to 'No se pudieron enumerar las herramientas:'.
        """
        template_text = PLAYGROUND_TEMPLATES_DIR.joinpath("mcp_browser.html").read_text(
            encoding="utf-8"
        )
        assert "No se pudieron enumerar las herramientas:" in template_text, (
            "mcp_browser.html must declare the Spanish error message "
            "'No se pudieron enumerar las herramientas:'"
        )

    def test_calling_status_message_is_spanish(self, web_client: object) -> None:
        """The JS-side 'Calling {name} ...' status message MUST be translated
        to 'Llamando {name}…'.
        """
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        assert "Llamando " in text, (
            "mcp_browser.html inline client must surface 'Llamando {name}…' "
            "(Spanish for 'Calling {name}…')"
        )


class TestMcpBrowserRenderedToolForms:
    """End-to-end render checks for /mcp-ui. Each tool exposed by the
    FastMCP composition root MUST render its own form with Spanish
    tool description, Spanish field labels and editable native input
    elements for required fields.

    This is the integration check for the
    ``mcp-ui-spanish-override`` change: the Python serializer
    (``mcp_browser._serialize_tools``) must read the FastMCP 3.4.6
    ``.parameters`` attribute and the Spanish override map must be
    applied end-to-end on the rendered HTML.
    """

    @pytest.fixture
    def mcp_ui_text(self, web_client: object) -> str:
        return web_client.get("/mcp-ui").text  # type: ignore[attr-defined]

    @staticmethod
    def _extract_tool_section(html: str, tool_name: str) -> str:
        """Return the <section data-tool-name="<tool>">…</section>
        block (greedy match until the next section or end of body).
        """
        match = re.search(
            rf'<section[^>]*data-tool-name="{re.escape(tool_name)}".*?</section>',
            html,
            flags=re.DOTALL,
        )
        assert match, f"/mcp-ui response must render a <section> for tool {tool_name!r}"
        return match.group(0)

    @pytest.mark.parametrize(
        "tool_name,expected_description_fragment",
        [
            (
                "list_projects",
                "Lista los proyectos del portfolio",
            ),
            (
                "search_code",
                "Búsqueda semántica sobre los chunks de código",
            ),
            (
                "explain_architecture",
                "Resume la arquitectura de un proyecto",
            ),
            (
                "summarize_readme",
                "Resume el README de un proyecto",
            ),
            (
                "get_architecture_diagram",
                "Devuelve el diagrama de arquitectura",
            ),
            (
                "ask_portfolio",
                "Pregunta libre sobre el portfolio",
            ),
        ],
    )
    def test_tool_description_is_spanish(
        self,
        mcp_ui_text: str,
        tool_name: str,
        expected_description_fragment: str,
    ) -> None:
        """Every tool's <p> description MUST be Spanish (the override
        map wins over the English docstring). This is the recruiter-
        facing copy; English would be a regression.
        """
        section = self._extract_tool_section(mcp_ui_text, tool_name)
        # The description sits inside the first <p> after the <h2>.
        desc_match = re.search(r"<h2>[^<]*</h2>\s*<p>(.*?)</p>", section, re.DOTALL)
        assert desc_match, f"{tool_name} section must render a <p> description right after <h2>"
        assert expected_description_fragment in desc_match.group(1), (
            f"{tool_name} description must be Spanish; expected fragment "
            f"{expected_description_fragment!r}, got {desc_match.group(1)!r}"
        )

    @pytest.mark.parametrize(
        "tool_name,expected_field_labels",
        [
            ("search_code", ["Consulta", "Cantidad de resultados", "ID del proyecto"]),
            ("explain_architecture", ["ID del proyecto", "Tokens máximos"]),
            ("summarize_readme", ["ID del proyecto", "Tokens máximos"]),
            ("get_architecture_diagram", ["ID del proyecto"]),
            ("ask_portfolio", ["Pregunta", "ID de conversación"]),
        ],
    )
    def test_field_labels_are_spanish(
        self,
        mcp_ui_text: str,
        tool_name: str,
        expected_field_labels: list[str],
    ) -> None:
        """Every required (and optional) field on the rendered form
        MUST show its Spanish label. Confirms the per-field override
        map is being applied during _serialize_tools.
        """
        section = self._extract_tool_section(mcp_ui_text, tool_name)
        for label in expected_field_labels:
            assert label in section, (
                f"{tool_name} form must render the Spanish field label "
                f"{label!r}; section body was:\n{section[:600]}"
            )

    def test_search_code_required_field_has_editable_input(self, mcp_ui_text: str) -> None:
        """The required `query` field on search_code MUST render an
        editable <input type="text" required> — not a textarea, not a
        disabled field. This is the bug regression guard: pre-fix the
        whole form showed the "No hay campos declarados" fallback.
        """
        section = self._extract_tool_section(mcp_ui_text, "search_code")
        # Look for the required text input for the `query` field.
        input_match = re.search(
            r'<input[^>]*type="text"[^>]*name="query"[^>]*>',
            section,
            flags=re.DOTALL,
        )
        assert input_match, (
            'search_code form MUST render a native <input type="text" '
            'name="query"> — the bug was empty fields, the fix exposes them'
        )
        attrs = input_match.group(0)
        assert "required" in attrs, (
            "the search_code `query` input must declare `required` "
            "(it's a required field per the schema)"
        )

    @pytest.mark.parametrize(
        "tool_name,required_field_name",
        [
            ("search_code", "query"),
            ("explain_architecture", "project_id"),
            ("summarize_readme", "project_id"),
            ("get_architecture_diagram", "project_id"),
            ("ask_portfolio", "question"),
        ],
    )
    def test_required_field_renders_editable_input(
        self,
        mcp_ui_text: str,
        tool_name: str,
        required_field_name: str,
    ) -> None:
        """The required field on each tool MUST render as an editable
        <input>. Pre-fix the page showed the "No hay campos declarados"
        placeholder for every tool except list_projects. Post-fix every
        tool with fields shows them as native inputs.
        """
        section = self._extract_tool_section(mcp_ui_text, tool_name)
        input_match = re.search(
            rf'<input[^>]*name="{re.escape(required_field_name)}"[^>]*>',
            section,
            flags=re.DOTALL,
        )
        assert input_match, (
            f"{tool_name} form must render an editable <input> for its "
            f"required field {required_field_name!r} (got section:\n"
            f"{section[:800]})"
        )
        assert "required" in input_match.group(0), (
            f"{tool_name} {required_field_name} input must declare `required` per the schema"
        )

    def test_list_projects_renders_no_field_placeholder(self, mcp_ui_text: str) -> None:
        """list_projects has no fields per the schema, so it MUST
        render the existing "No hay campos declarados" placeholder —
        this confirms the empty-schema path still works post-fix
        (regression guard for the back-compat branch).
        """
        section = self._extract_tool_section(mcp_ui_text, "list_projects")
        assert "No hay campos declarados" in section, (
            "list_projects has no fields and must render the empty-form "
            "placeholder; this guards against accidentally synthesising "
            "fake fields when the schema is empty"
        )


# ---------------------------------------------------------------------------
# Task 3 — chat composer bottom margin (visual breathing room)
# ---------------------------------------------------------------------------


class TestChatComposerBottomMargin:
    """The chat composer sits at the bottom of the viewport with no
    breathing room — looks cramped. The CSS MUST give the ``.chat-form``
    a bottom margin (or the ``.playground-chat`` section a bottom
    padding) so the composer visually separates from the page edge.
    """

    def test_css_defines_chat_form_bottom_margin(self) -> None:
        """``playground/static/style.css`` MUST define a ``.chat-form``
        rule with a non-zero bottom margin (or bottom padding on the
        chat section) so the composer has visible breathing room.
        """
        css_path = PLAYGROUND_STATIC_DIR / "style.css"
        css = css_path.read_text(encoding="utf-8")
        # Pull the .chat-form rule body and check for a bottom margin
        # declaration. The rule must be non-empty (a class with no
        # declarations is dead code).
        form_match = re.search(r"\.chat-form\s*\{([^}]*)\}", css, flags=re.DOTALL)
        assert form_match, (
            "style.css must define a .chat-form rule; the chat composer's "
            "visual layout depends on it"
        )
        body = form_match.group(1).strip()
        assert body, (
            "style.css's .chat-form rule is empty; the visual fix "
            "needs a margin-bottom or padding-bottom declaration"
        )
        # Accept either margin-bottom or padding-bottom. Both are
        # valid visual fixes — the spec says "use whatever looks
        # balanced".
        assert re.search(r"margin-bottom\s*:", body) or re.search(r"padding-bottom\s*:", body), (
            "style.css's .chat-form rule must have a margin-bottom or "
            "padding-bottom declaration so the composer has breathing room"
        )

    def test_css_does_not_let_chat_form_sit_flush_against_page_edge(self) -> None:
        """The chat-form's bottom margin OR the chat section's bottom
        padding MUST be > 0. Negative or zero values would defeat the
        purpose of the visual fix.
        """
        css_path = PLAYGROUND_STATIC_DIR / "style.css"
        css = css_path.read_text(encoding="utf-8")
        form_match = re.search(r"\.chat-form\s*\{([^}]*)\}", css, flags=re.DOTALL)
        assert form_match, "style.css must define a .chat-form rule"
        body = form_match.group(1)
        # Extract any margin-bottom or padding-bottom declaration.
        margin_m = re.search(r"margin-bottom\s*:\s*([^;]+);", body)
        padding_m = re.search(r"padding-bottom\s*:\s*([^;]+);", body)
        assert margin_m or padding_m, (
            "style.css's .chat-form rule must declare a margin-bottom or padding-bottom"
        )
        # Convert the value to a number and assert > 0.
        for m in (margin_m, padding_m):
            if m is None:
                continue
            raw = m.group(1).strip()
            # Strip trailing CSS units (rem, em, px).
            numeric = re.match(r"^(-?\d+\.?\d*)", raw)
            assert numeric, (
                f"could not parse margin/padding value {raw!r}; use a CSS length (rem, em, px)"
            )
            assert float(numeric.group(1)) > 0, (
                f"chat-form margin/padding-bottom must be > 0; got {raw!r}"
            )


# ---------------------------------------------------------------------------
# Task 4 — clear-history button in chat
# ---------------------------------------------------------------------------


class TestChatClearHistoryButton:
    """The chat has no way to clear the conversation history from the
    UI (must use DevTools). Add a small "Limpiar" button next to the
    Send button with a ``clearHistory()`` JS handler that wipes
    localStorage and the transcript.
    """

    @pytest.mark.asyncio
    async def test_clear_history_button_is_rendered_in_chat_html(self, chat_client_factory) -> None:
        """chat.html MUST render a clear-history button with a stable
        marker id (``chat-clear``) AND a Spanish label ("Limpiar" or
        "Borrar historial").
        """
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        body = response.text
        # The button MUST be inside the <form id="chat-form">. We
        # accept either an id="chat-clear" attribute or a
        # class="chat-clear" attribute (the spec offers both; the
        # minimum contract is "a clear-history button is identifiable").
        assert 'id="chat-clear"' in body, (
            'chat.html must render a clear-history button with id="chat-clear"'
        )
        assert 'class="chat-clear"' in body, (
            'chat.html must render a clear-history button with class="chat-clear"'
        )
        # The button's text MUST be in Spanish ("Limpiar" or "Borrar historial").
        assert "Limpiar" in body or "Borrar historial" in body, (
            "the clear-history button label must be in Spanish ('Limpiar' or 'Borrar historial')"
        )

    @pytest.mark.asyncio
    async def test_clear_history_js_handler_is_wired(self, chat_client_factory) -> None:
        """The inline JS MUST define a ``clearHistory()`` function that
        wipes localStorage and clears the transcript, and the button
        MUST be wired to it via addEventListener.
        """
        async with chat_client_factory() as client:
            response = await client.get("/chat")
        body = response.text

        script_match = re.search(r"<script>(.*?)</script>", body, re.DOTALL)
        assert script_match, "chat page must include an inline <script> block"
        script = script_match.group(1)

        # The clearHistory function MUST be defined.
        assert re.search(r"function\s+clearHistory\s*\(", script), (
            "chat.html must define a clearHistory() function"
        )
        # The handler MUST wipe localStorage (remove the historyKey entry).
        assert "removeItem" in script, (
            "clearHistory() must call localStorage.removeItem to wipe the persisted history"
        )
        # The handler MUST clear the transcript DOM.
        assert "transcript" in script, (
            "clearHistory() must reach the transcript element to clear it"
        )
        # The handler MUST be wired to the button via addEventListener.
        assert "addEventListener" in script, (
            "the clear-history button must be wired via addEventListener"
        )
        # The confirm() call MUST be in Spanish ("Borrar" or "¿").
        assert "¿Borrar" in script and "?" in script, (
            "clearHistory() must prompt the user in Spanish before wiping"
        )

    def test_css_defines_chat_clear_button_rule(self) -> None:
        """style.css MUST define a ``.chat-clear`` rule for the new
        button. The button is a secondary outline (transparent bg,
        cyan border, base01 text) — not a solid cyan like the Send
        button — so the visual hierarchy is clear.
        """
        css_path = PLAYGROUND_STATIC_DIR / "style.css"
        css = css_path.read_text(encoding="utf-8")
        rule_match = re.search(r"\.chat-clear\s*\{([^}]*)\}", css, flags=re.DOTALL)
        assert rule_match, (
            "style.css must define a .chat-clear rule for the new clear-history button"
        )
        body = rule_match.group(1).strip()
        assert body, (
            "style.css's .chat-clear rule is empty; the visual style is part of the contract"
        )
        # Palette discipline: the border MUST use the existing
        # --solar-cyan accent (no new colors per the change rules).
        assert "var(--solar-cyan)" in body, (
            ".chat-clear border must use var(--solar-cyan); the locked "
            "Solarized Phosphor palette forbids new colors"
        )


class TestMcpBrowserResultRenderers:
    """The /mcp-ui page must ship semantic result renderers for recruiters."""

    def test_renderer_dispatcher_and_functions_are_rendered(self, web_client: object) -> None:
        """The inline client must expose one dispatcher, five tool renderers,
        and a readable fallback renderer.
        """
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        for marker in (
            "RENDERERS",
            "renderProjectCards",
            "renderSearchResultCards",
            "renderSummary",
            "renderArchitectureDiagram",
            "renderDefault",
        ):
            assert marker in text, f"/mcp-ui must render the {marker} renderer marker"

    def test_renderer_css_classes_are_rendered(self, web_client: object) -> None:
        """The inline renderer source must contain the semantic result classes."""
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        for marker in (
            "mcp-result-cards",
            "mcp-result-card",
            "mcp-result-snippet",
            "mcp-result-badge",
            "mcp-result-summary",
            "mcp-result-sources",
            "mcp-result-diagram",
            "mcp-empty-state",
        ):
            assert marker in text, f"/mcp-ui must render the {marker} class marker"

    def test_renderSummary_handles_singular_source_field(self, web_client: object) -> None:
        """summarize_readme returns ``source`` (singular) not ``sources`` (plural);
        renderSummary must normalize so the sources row never silently disappears.
        """
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        assert "summary.source" in text, (
            "/mcp-ui renderer must read the singular ``source`` field "
            "produced by summarize_readme (in addition to plural ``sources``)"
        )

    def test_renderArchitectureDiagram_reads_data_field(self, web_client: object) -> None:
        """get_architecture_diagram returns base64 SVG in ``data``; the renderer
        must read that field, not a phantom ``display_svg``.
        """
        text = web_client.get("/mcp-ui").text  # type: ignore[attr-defined]
        assert "diagram.data" in text, (
            "/mcp-ui renderer must read the ``data`` field from "
            "GetArchitectureDiagramResult (base64-encoded SVG)"
        )
        assert "display_svg" not in text, (
            "/mcp-ui renderer must NOT reference the phantom ``display_svg`` "
            "field — only ``data`` exists in the dataclass"
        )
