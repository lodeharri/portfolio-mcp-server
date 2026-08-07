"""Integration tests for the browser-facing routes.

The web router exposes the landing page (``GET /``), the auto-generated
MCP explorer (``GET /mcp-ui``), and the streaming chat surface
(``GET /chat`` + ``POST /chat/stream``). These integration tests
exercise the wired app via :class:`fastapi.testclient.TestClient` and
assert:

* ``GET /`` returns 200 ``text/html``, lists every manifest-declared
  project, carries the CTAs (``/mcp-ui`` + ``/chat``), and renders
  without 500 even when the SQLite vector index is absent
  (``index_chunk_count == 0`` fallback).
* ``GET /mcp-ui`` returns 200 ``text/html`` and is the sole
  browser-facing tool surface (the old ``/playground`` form-cards
  page and its five form endpoints were removed in Phase 2).
* The web router is mounted at the documented position: between
  ``/healthz`` (existing) and ``/mcp`` (existing), and
  ``create_app(...).url_path_for(\"landing\")`` resolves ``/``.

Per change 003-playground-ui tasks 1.4.3, 1.5.2, 1.6.4.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PLAYGROUND_TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "playground" / "templates"


@pytest.fixture(scope="module")
def app():
    from mcp_server.app import create_app
    from mcp_server.config import AppConfig

    return create_app(AppConfig(gemini_api_key=""))


@pytest.fixture(scope="module")
def client(app):
    from fastapi.testclient import TestClient

    # ``TestClient(app)`` enters the FastMCP lifespan so /mcp is
    # fully bootable; without the context manager the lifespan event
    # handlers never fire and FastMCP raises "task group not
    # initialized".
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# GET / — landing page
# ---------------------------------------------------------------------------


class TestLandingRoute:
    def test_landing_returns_200_html(self, client: object) -> None:
        response = client.get("/")  # type: ignore[attr-defined]
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    def test_landing_extends_base_template(self, client: object) -> None:
        """Every page MUST extend ``playground/templates/base.html``."""
        text = client.get("/").text  # type: ignore[attr-defined]
        assert '<link rel="stylesheet" href="/static/style.css">' in text
        # REL-10: the HTMX <script> tag now carries an SRI integrity +
        # crossorigin attribute (single-line in PR1; multi-line in PR2b
        # to keep the long sha384 hash readable). Assert the asset URL
        # is referenced rather than the exact single-line format.
        assert 'src="/static/htmx.min.js"' in text

    def test_landing_renders_project_list_with_one_anchor_per_id(self, client: object, app) -> None:
        """The manifest's projects MUST each appear as a clickable anchor."""
        projects = list(app.state.composition.manifest.projects())
        if not projects:
            pytest.skip("manifest declares zero projects; landing list check is vacuous")

        text = client.get("/").text  # type: ignore[attr-defined]
        for project in projects:
            assert project.id in text, f"landing page must surface project id {project.id!r}"

    def test_landing_has_mcp_ui_cta_and_chat_cta(self, client: object) -> None:
        """The two CTAs (``/mcp-ui`` explorer + ``/chat``) MUST be present
        on the landing page. ``/playground`` is gone (Phase 2 cleanup).
        """
        text = client.get("/").text  # type: ignore[attr-defined]
        assert 'href="/mcp-ui"' in text
        assert 'href="/chat"' in text

    def test_landing_renders_with_zero_index(self, client: object) -> None:
        """When ``data/index.sqlite`` is absent, ``index_chunk_count``
        defaults to ``0`` for every project and the page MUST still 200.
        """
        # The composition root tolerates a missing vector store (it
        # constructs an open SqliteVecStore that handles the empty
        # table), so the page MUST be 200 even when the index is empty.
        response = client.get("/")  # type: ignore[attr-defined]
        assert response.status_code == 200
        # Either the page declares a project with chunks=0, OR the
        # manifest is empty. We don't assert concrete count to stay
        # tolerant to sibling projects that may be added in the future.
        assert "playground-projects" in response.text or "No projects declared" in response.text


# ---------------------------------------------------------------------------
# Web router skeleton
# ---------------------------------------------------------------------------


class TestPlaygroundSurfaceRemoved:
    """Phase 2 cleanup — ``/playground`` and the five form endpoints were
    removed because ``/mcp-ui`` is the sole browser-facing tool surface.

    The routes must 404 (not 405, not 500); the form endpoints must
    not accept POSTs that hit use cases directly. ``/mcp-ui`` remains.
    """

    def test_get_playground_returns_404(self, client: object) -> None:
        response = client.get("/playground")  # type: ignore[attr-defined]
        assert response.status_code == 404, (
            "GET /playground must 404 — /mcp-ui is the sole browser tool surface"
        )

    def test_post_playground_api_search_code_returns_404(self, client: object) -> None:
        response = client.post(  # type: ignore[attr-defined]
            "/playground/api/search_code",
            data={"query": "x"},
        )
        assert response.status_code == 404, (
            "POST /playground/api/search_code must 404 — the form endpoint "
            "surface is gone; callers must use the /mcp JSON-RPC transport"
        )

    def test_post_playground_api_list_projects_returns_404(self, client: object) -> None:
        response = client.post("/playground/api/list_projects")  # type: ignore[attr-defined]
        assert response.status_code == 404, (
            "POST /playground/api/list_projects must 404 — surface removed"
        )

    def test_mcp_ui_still_200(self, client: object) -> None:
        """Sanity check: the surviving tool surface is still reachable."""
        response = client.get("/mcp-ui")  # type: ignore[attr-defined]
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")


class TestWebRouterSkeleton:
    def test_landing_route_is_named(self, app) -> None:
        """``app.url_path_for(\"landing\")`` MUST resolve ``/``
        (the spec scenario "create_app().url_path_for('landing')").
        """
        assert app.url_path_for("landing") == "/"

    def test_web_router_present_without_altering_mcp_mount(self, app, client) -> None:
        """The web router MUST be mounted, and ``/mcp`` MUST still
        route to the FastMCP sub-app (mount position preserved).
        """
        # url_path_for already confirms the landing route resolves to /.
        assert app.url_path_for("landing") == "/"
        # /healthz still answers (no route collision with the new mount).
        assert client.get("/healthz").status_code == 200  # type: ignore[attr-defined]
        # /mcp sub-app is still mounted — a vanilla GET returns either 406
        # (missing Accept header), 415, or 400 depending on the MCP server
        # version. Any of these confirms the mount is reachable; a 404
        # would mean the web router stole the path.
        mcp_response = client.get("/mcp")  # type: ignore[attr-defined]
        assert mcp_response.status_code != 404, (
            f"/mcp sub-app must remain mounted; got {mcp_response.status_code}"
        )


# ---------------------------------------------------------------------------
# PR2b — streaming chat surface (mounting test + integration-level SSE)
# ---------------------------------------------------------------------------


class TestChatRoutesWired:
    """The chat router is mounted on ``build_web_router()``.

    These tests verify the wire-in step from PR2b task 2b.4.2: the
    routes added by ``build_chat_router()`` are reachable through the
    full ``create_app()`` composition (not just the standalone router
    fixtures used in the unit tests).
    """

    def test_chat_page_route_is_named(self, app) -> None:
        """``app.url_path_for('chat_page')`` MUST resolve ``/chat``."""
        assert app.url_path_for("chat_page") == "/chat"

    def test_chat_stream_route_is_named(self, app) -> None:
        """``app.url_path_for('chat_stream')`` MUST resolve ``/chat/stream``."""
        assert app.url_path_for("chat_stream") == "/chat/stream"

    def test_get_chat_returns_200_html_through_full_app(self, client: object) -> None:
        """``GET /chat`` through ``create_app()`` returns 200 with ``text/html``.

        Confirms the chat router is wired and reaches the same Jinja2
        environment as the landing / playground surfaces (the page
        extends ``base.html``).
        """
        response = client.get("/chat")  # type: ignore[attr-defined]
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        text = response.text
        # base.html assets render through any chat page; the inline
        # client script is the PR2b signature asset.
        assert "/static/style.css" in text
        assert "/static/htmx.min.js" in text
        assert "<script>" in text
        assert "/chat/stream" in text


class TestChatStreamingE2E:
    """``POST /chat/stream`` delivers a real SSE stream through ``create_app()``.

    PR2b acceptance gate — reads ≥ 2 chunks within 5 seconds in mock
    mode (no ``GEMINI_API_KEY``). Uses
    :class:`fastapi.testclient.TestClient` ``stream(...)`` which
    matches the ``httpx.AsyncClient.stream(...)`` shape from
    integration-tests-and-deploy runs; the in-process TestClient is
    sufficient and avoids the asynctest loop overhead.
    """

    def test_post_chat_stream_delivers_at_least_two_chunks_within_five_seconds(
        self, client: object
    ) -> None:
        """End-to-end streaming smoke through the wired composition.

        Mock agent yields 5 tokens spaced 50 ms apart; 2 chunks
        arrive within ~100 ms well under the 5 s gate.
        """
        import time

        import pytest

        data_events: list[str] = []
        start = time.monotonic()
        with client.stream(  # type: ignore[attr-defined]
            "POST",
            "/chat/stream",
            json={
                "messages": [{"role": "user", "content": "demo"}],
                "conversation_id": "pr2b-e2e",
            },
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            for line in response.iter_lines():
                if line.startswith("data:"):
                    data_events.append(line)
                if line == "data: [DONE]":
                    break
                if time.monotonic() - start > 5.0:
                    pytest.fail("stream did not finish within 5 s budget")
        elapsed = time.monotonic() - start

        assert len(data_events) >= 2, (
            f"expected >=2 streamed data: events within 5s; got {len(data_events)}; "
            f"elapsed={elapsed:.2f}s"
        )
        # Mock token sequence must be present (post-sanitize).
        joined = "\n".join(data_events)
        assert "Tok" in joined
        assert "answer" in joined
        assert "[DONE]" in joined
        # Stream terminates with the sentinel (no partial token after).
        assert data_events[-1] == "data: [DONE]", (
            f"stream must end on [DONE]; tail={data_events[-3:]!r}"
        )

    def test_post_chat_stream_response_carries_no_set_cookie_header(self, client: object) -> None:
        """Privacy contract — no ``Set-Cookie`` header on the chat stream.

        Spec scenario "No Set-Cookie header on /chat/stream" — the
        server is stateless by design and sets no cookies.
        """
        response = client.post(  # type: ignore[attr-defined]
            "/chat/stream",
            json={
                "messages": [{"role": "user", "content": "x"}],
                "conversation_id": "no-cookie",
            },
        )
        assert response.status_code == 200
        assert response.headers.get("set-cookie") is None

    def test_post_chat_stream_with_full_history_round_trip(self, client: object) -> None:
        """The request body MUST echo the full ``messages`` array (stateful-client contract).

        Spec scenario "Full Messages Sent Per Request" — the server
        NEVER short-circuits history. This test sends a 4-turn
        conversation and confirms the stream still completes.
        """
        conversation = [
            {"role": "user", "content": "What's your first project?"},
            {
                "role": "assistant",
                "content": "land-page-portfolio. It's a Next.js site.",
            },
            {"role": "user", "content": "What tech stack does it use?"},
            {
                "role": "assistant",
                "content": "Next.js 14, TypeScript, Tailwind.",
            },
            {"role": "user", "content": "Anything else?"},
        ]

        response = client.post(  # type: ignore[attr-defined]
            "/chat/stream",
            json={
                "messages": conversation,
                "conversation_id": "history-rt",
            },
        )
        assert response.status_code == 200
        # The mock stream produces a complete token sequence regardless of history.
        assert "data: [DONE]" in response.text
