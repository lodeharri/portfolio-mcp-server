"""Unit tests for the streaming ``chat`` routes (PR2b surface).

Two route handlers under test (PR2b task 2b.3):

* ``GET /chat`` — renders ``playground/templates/chat.html`` with the
  browser client wiring inline (``fetch`` + ``ReadableStream`` SSE
  parsing, ``localStorage`` history with the namespaced keys from the
  ``chat-persistence`` spec, and the no-JS graceful-degradation
  affordance).
* ``POST /chat/stream`` — accepts ``{"messages": [...],
  "conversation_id": "..."}``, invokes ``AskPortfolioUseCase.astream``,
  and wraps each ``AskPortfolioChunk`` as one Server-Sent Event. The
  terminal chunks map to:

    * ``kind="done"``  → ``data: [DONE]\n\n`` (success sentinel)
    * ``kind="error"`` → ``data: [ERROR]\n\n`` (failure sentinel; REL-3)
    * ``kind="token"`` → ``data: <token>\n\n``
    * ``kind="tool_call"`` → ``event: tool_call\ndata: {"name": ...}\n\n``

These tests use :func:`mcp_server.interfaces.http.web.chat.build_chat_router`
directly (the unit under test is the standalone router); the
integration-level wiring is asserted in
``tests/integration/test_web_routes.py::TestChatRoutesE2E``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from mcp_server.application.use_cases.ask_portfolio import (
    AskPortfolioChunk,
    AskPortfolioRequest,
    AskPortfolioResult,
)


def _make_app(router: FastAPI | None = None) -> FastAPI:
    """Build a minimal FastAPI app and mount the chat router for unit tests.

    The chat routes use ``request.app.state.composition`` to look up
    ``AskPortfolioUseCase``. The unit-level fixtures below either mount
    a tiny synthetic composition directly or stub the request state.
    """
    from mcp_server.application.ports.agent import AgentPort

    class _StubAskPortfolio:
        """Minimal use-case stub that consumes ``ask_portfolio_use_case.astream`` calls.

        Yields the supplied chunks (defaulting to the canonical 5-token
        mock-stream shape) so each test can wire a deterministic SSE
        payload.
        """

        def __init__(self, chunks: list[AskPortfolioChunk] | None = None) -> None:
            self.chunks = chunks if chunks is not None else _mock_chunks()

        async def astream(self, request: AskPortfolioRequest) -> AsyncIterator[AskPortfolioChunk]:
            for chunk in self.chunks:
                yield chunk

    class _StubComposition:
        ask_portfolio_use_case: AgentPort | None | _StubAskPortfolio  # type: ignore[assignment]
        sanitizer = None  # chat route does not consult sanitizer

    stub = _StubComposition()
    stub.ask_portfolio_use_case = _StubAskPortfolio()
    app = FastAPI()
    app.state.composition = stub  # type: ignore[attr-defined]
    app.include_router(router)
    return app


def _mock_chunks() -> list[AskPortfolioChunk]:
    """Return the canonical 5-token + done chunk stream from the mock agent.

    Mirrors the mock ``_MockLangChainAgentAdapter`` output so the SSE
    encoder can be tested without a Gemini key.
    """
    return [
        AskPortfolioChunk(kind="token", answer_token="Tok"),
        AskPortfolioChunk(kind="token", answer_token="en"),
        AskPortfolioChunk(kind="token", answer_token="ized"),
        AskPortfolioChunk(kind="token", answer_token=" mock"),
        AskPortfolioChunk(kind="token", answer_token=" answer"),
        AskPortfolioChunk(
            kind="done",
            result=AskPortfolioResult(answer="Tokenized mock answer"),
        ),
    ]


@pytest.fixture
def chat_router():
    """Build the standalone chat router (returns a fresh APIRouter)."""
    from mcp_server.interfaces.http.web.chat import build_chat_router

    return build_chat_router()


# ---------------------------------------------------------------------------
# RED tests — chat route handlers (PR2b task 2b.3.1 / 2b.3.3)
# ---------------------------------------------------------------------------


class TestGetChatPage:
    """GET /chat returns the chat UI template."""

    @pytest.mark.asyncio
    async def test_get_chat_returns_200_html(self, chat_router) -> None:
        """The chat page MUST respond with HTTP 200 and ``text/html``."""
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")

    @pytest.mark.asyncio
    async def test_get_chat_renders_input_form(self, chat_router) -> None:
        """The page MUST render the chat input form (textbox + submit)."""
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text
        # The chat UI needs an input the recruiter types into and a
        # submit button that drives the SSE fetch.
        assert "<form" in body, "chat page must render a form"
        assert 'name="message"' in body or 'id="chat-input"' in body
        assert ">Send<" in body or 'type="submit"' in body

    @pytest.mark.asyncio
    async def test_get_chat_embeds_inline_client_script(self, chat_router) -> None:
        """The chat client JS MUST be embedded inline in ``<script>`` so the
        template is self-contained — no separate ``chat.js`` static.
        """
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text
        # Must contain a real, non-trivial inline script (not just a
        # <script src="..."> reference).
        assert "<script>" in body, "chat page must embed an inline script block"
        # The inline script MUST POST to /chat/stream and parse SSE.
        assert "/chat/stream" in body
        assert ("fetch(" in body) or ("EventSource" in body)

    @pytest.mark.asyncio
    async def test_get_chat_extends_base_template(self, chat_router) -> None:
        """The page MUST extend ``base.html`` so navigation / stylesheets line up."""
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text
        # base.html includes the HTMX script tag in <head>; the chat
        # page extends it via Jinja2 (regardless of whether the chat
        # client uses HTMX, the nav + style.css must be present).
        assert "/static/style.css" in body
        assert "/static/htmx.min.js" in body

    @pytest.mark.asyncio
    async def test_get_chat_declares_localstorage_keys(self, chat_router) -> None:
        """The inline script MUST namespace the localStorage history under
        ``mcp-playground-chat:<uuid>:history`` per the ``chat-persistence``
        spec.
        """
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text
        assert "mcp-playground-chat" in body
        # The ':sid' and ':history' suffixes from the spec must appear.
        assert ":sid" in body
        assert ":history" in body

    @pytest.mark.asyncio
    async def test_get_chat_localstorage_round_trip_primitives(self, chat_router) -> None:
        """The inline script MUST wire a full localStorage round-trip.

        PR2b acceptance gate: ``write history → read back → assert
        equal``. We assert the JS primitives are present: ``crypto.randomUUID``
        for the session UUID, ``JSON.stringify`` for serializing messages,
        ``JSON.parse`` for deserializing them, ``localStorage.setItem`` /
        ``localStorage.getItem`` for the round-trip itself, and a
        defensive ``try/catch`` around both calls for graceful
        degradation in private mode.
        """
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text
        # Session UUID generation
        assert "crypto.randomUUID" in body
        # Round-trip primitives — both write and read paths exist
        assert "JSON.stringify" in body
        assert "JSON.parse" in body
        assert "localStorage.setItem" in body
        assert "localStorage.getItem" in body
        # Try/catch around storage operations (graceful degradation)
        assert "try" in body
        assert "catch" in body

    @pytest.mark.asyncio
    async def test_get_chat_sends_full_messages_array_in_post(self, chat_router) -> None:
        """The inline script MUST POST the full ``messages`` array.

        Spec scenario "Nth turn sends a length-N messages array" — the
        client concatenates the persisted history with the new user
        message before POSTing. We assert the loadHistory → POST payload
        shape exists.
        """
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text
        # The script MUST JSON-serialize a payload containing `messages` and
        # `conversation_id` keys.
        assert '"messages"' in body or "messages:" in body
        assert "conversation_id" in body
        # Headers explicitly set per spec.
        assert "Accept" in body
        assert "text/event-stream" in body


class TestPostChatStream:
    """POST /chat/stream wraps the astream use case as SSE."""

    @pytest.mark.asyncio
    async def test_post_chat_stream_returns_text_event_stream(self, chat_router) -> None:
        """The route MUST respond with ``text/event-stream`` so the browser
        fetch reads bytes incrementally (not buffered HTML).
        """
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/chat/stream",
                json={
                    "messages": [{"role": "user", "content": "hello"}],
                    "conversation_id": "abc",
                },
            )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

    @pytest.mark.asyncio
    async def test_post_chat_stream_emits_at_least_two_data_events(self, chat_router) -> None:
        """The body MUST contain at least two ``data:`` events — one per
        token chunk (the mock yields 5 + 1 done = 6 events).
        """
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/chat/stream",
                json={
                    "messages": [{"role": "user", "content": "test"}],
                    "conversation_id": "x",
                },
            )
        body = response.text
        data_events = [line for line in body.splitlines() if line.startswith("data:")]
        assert len(data_events) >= 2, (
            f"expected >=2 data: events, got {len(data_events)}; body={body!r}"
        )

    @pytest.mark.asyncio
    async def test_post_chat_stream_terminates_with_done_sentinel(self, chat_router) -> None:
        """The stream MUST terminate with ``data: [DONE]\\n\\n`` after the
        use case's ``done`` chunk is consumed.
        """
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/chat/stream",
                json={
                    "messages": [{"role": "user", "content": "q"}],
                    "conversation_id": "y",
                },
            )
        body = response.text
        assert "data: [DONE]" in body
        # The DONE event MUST be the last meaningful line in the stream.
        stripped = body.strip()
        assert stripped.endswith("data: [DONE]"), (
            f"stream must terminate with DONE sentinel; tail={stripped[-80:]!r}"
        )

    @pytest.mark.asyncio
    async def test_post_chat_stream_maps_error_chunk_to_error_sentinel(self, chat_router) -> None:
        """When the use case yields ``kind='error'`` (REL-3), the route MUST
        emit ``data: [ERROR]\\n\\n`` as the terminal event.

        This is the contract the browser client relies on to render the
        inline "connection lost, retry?" affordance.
        """
        from mcp_server.application.use_cases.ask_portfolio import (
            AskPortfolioUseCase,
        )

        class _ErrorStubAskPortfolio(AskPortfolioUseCase):
            async def astream(
                self, request: AskPortfolioRequest
            ) -> AsyncIterator[AskPortfolioChunk]:
                yield AskPortfolioChunk(kind="token", answer_token="partial ")
                yield AskPortfolioChunk(kind="error", error="agent exploded")

        app = _make_app(chat_router)
        # Swap the stub composition's use case for one that yields an
        # error chunk mid-stream.
        app.state.composition.ask_portfolio_use_case = _ErrorStubAskPortfolio(  # type: ignore[assignment]
            agent=None,  # type: ignore[arg-type]
            tools=[],
            sanitizer=None,  # type: ignore[arg-type]
            audit=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/chat/stream",
                json={
                    "messages": [{"role": "user", "content": "x"}],
                    "conversation_id": "z",
                },
            )
        body = response.text
        # The partial token MAY appear; what MUST appear is the error
        # sentinel — the terminal event for the error path.
        assert "data: [ERROR]" in body
        # The terminal sentinel is the last meaningful event; neither
        # DONE nor further tokens may follow.
        stripped = body.strip()
        assert stripped.endswith("data: [ERROR]"), (
            f"error stream must terminate with ERROR sentinel; tail={stripped[-80:]!r}"
        )
        # And no spurious DONE event after the error.
        assert body.count("data: [DONE]") == 0

    @pytest.mark.asyncio
    async def test_post_chat_stream_renders_token_events(self, chat_router) -> None:
        """Each ``AskPortfolioChunk(kind='token', answer_token=...)`` MUST
        become one ``data: <token>\\n\\n`` line, preserving the
        concatenated answer text.
        """
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/chat/stream",
                json={
                    "messages": [{"role": "user", "content": "compose"}],
                    "conversation_id": "tok",
                },
            )
        body = response.text
        # Mock yields "Tok", "en", "ized", " mock", " answer". Each MUST
        # appear as a separate data: line (the client concatenates them).
        for token in ("Tok", "en", "ized", " mock", " answer"):
            assert f"data: {token}" in body, (
                f"token {token!r} missing from SSE stream; body={body!r}"
            )

    @pytest.mark.asyncio
    async def test_post_chat_stream_renders_tool_call_event(self, chat_router) -> None:
        """When the use case yields ``kind='tool_call'``, the route MUST
        emit a typed SSE event with a ``tool_call`` event channel and
        a JSON payload carrying the tool name.
        """
        from mcp_server.application.use_cases.ask_portfolio import (
            AskPortfolioUseCase,
        )

        class _ToolCallStubAskPortfolio(AskPortfolioUseCase):
            async def astream(
                self, request: AskPortfolioRequest
            ) -> AsyncIterator[AskPortfolioChunk]:
                yield AskPortfolioChunk(
                    kind="tool_call",
                    tool_call={"name": "list_projects"},
                )
                yield AskPortfolioChunk(
                    kind="done",
                    result=AskPortfolioResult(answer="tools fired"),
                )

        app = _make_app(chat_router)
        app.state.composition.ask_portfolio_use_case = _ToolCallStubAskPortfolio(  # type: ignore[assignment]
            agent=None,  # type: ignore[arg-type]
            tools=[],
            sanitizer=None,  # type: ignore[arg-type]
            audit=None,  # type: ignore[arg-type]
            rate_limiter=None,  # type: ignore[arg-type]
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/chat/stream",
                json={
                    "messages": [{"role": "user", "content": "x"}],
                    "conversation_id": "tc",
                },
            )
        body = response.text
        assert "event: tool_call" in body, f"tool_call SSE event channel missing; body={body!r}"
        # The data line carries a JSON payload with the tool name.
        tool_data_lines = [
            line
            for line in body.splitlines()
            if line.startswith("data: ") and "list_projects" in line
        ]
        assert tool_data_lines, f"tool_call event data line missing; body={body!r}"
        # Parse the JSON payload to confirm the wire format.
        payload = json.loads(tool_data_lines[0].split("data: ", 1)[1])
        assert payload.get("name") == "list_projects"

    @pytest.mark.asyncio
    async def test_post_chat_stream_response_has_no_set_cookie_header(self, chat_router) -> None:
        """Privacy contract: no ``Set-Cookie`` header on ``/chat/stream``.

        The chat surface is anonymous and stateless — the browser owns
        the session UUID in localStorage; the server never sees it and
        never sets a cookie (spec scenario "No Set-Cookie header on
        /chat/stream").
        """
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.post(
                "/chat/stream",
                json={
                    "messages": [{"role": "user", "content": "x"}],
                    "conversation_id": "no-cookie",
                },
            )
        assert response.headers.get("set-cookie") is None, (
            f"chat route must not set cookies; got {response.headers.get('set-cookie')!r}"
        )

    @pytest.mark.asyncio
    async def test_post_chat_stream_reads_at_least_two_chunks_within_five_seconds(
        self, chat_router
    ) -> None:
        """Acceptance gate: streaming via real ``EventSourceResponse``
        delivers ≥ 2 chunks within 5 s in mock mode.

        Uses ``client.stream(...)`` so the iteration reads the bytes
        as they arrive (not a buffered ``post(...)``).
        """
        app = _make_app(chat_router)

        async def _drive() -> tuple[int, str]:
            data_events: list[str] = []
            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://testserver",
                timeout=5.0,
            ) as client:
                async with client.stream(
                    "POST",
                    "/chat/stream",
                    json={
                        "messages": [{"role": "user", "content": "fast"}],
                        "conversation_id": "speed",
                    },
                ) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            data_events.append(line)
                            if len(data_events) >= 2:
                                break
            return len(data_events), "\n".join(data_events)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + 5.0
        count, body = await _drive()
        elapsed_estimate = deadline - loop.time()
        # The mock yields 5 tokens in ~0.25s; the streamed response
        # MUST emit >=2 data: events well within 5s. Asserting loose
        # time bound — the test must not flake on a slow CI.
        assert count >= 2, (
            f"expected >=2 streamed data: events within 5s; got {count}; body={body!r}"
        )
        assert elapsed_estimate >= 0, "5s budget must be positive"
