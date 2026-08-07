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
          followed by ``event: error`` with a JSON message

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
    async def test_get_chat_renders_terminal_composer_and_accessibility_state(
        self, chat_router
    ) -> None:
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text
        assert 'class="chat-header"' in body
        assert 'class="chat-status__dot"' in body
        assert "<textarea" in body
        assert 'rows="1"' in body
        assert 'aria-busy="false"' in body
        assert 'class="chat-prompt"' in body
        assert "Enter to send" in body
        assert "Shift+Enter for newline" in body
        assert "→" in body
        assert "chat-typing" in body
        assert "resizeInput" in body

    @pytest.mark.asyncio
    async def test_get_chat_parser_distinguishes_typed_errors_from_connection_drops(
        self, chat_router
    ) -> None:
        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text
        assert "if (dataLine === ERROR)" in body
        assert 'eventName === "error"' in body
        assert "JSON.parse(dataLine)" in body
        assert "serverErrorMessage" in body
        assert 'showInlineRetry("Connection lost."' in body
        assert "showInlineRetry(serverErrorMessage" in body

    @pytest.mark.asyncio
    async def test_get_chat_renders_inline_trace_row_and_tool_pill_hook(self, chat_router) -> None:
        """The chat page MUST embed the JS hook that renders tool calls as
        inline pills above the assistant message body.

        Recruiter-UX gate: when the agent calls a sibling tool, the
        browser shows a small pill (``<name> "primary_arg"``) inside
        a ``.chat-trace`` row that lives above the assistant's prose.
        This is the affordance that lets a non-technical viewer
        confirm "yes, this is a real RAG agent that fetched code,
        not a fixed-text chatbot".

        The chat client is JS-only (the template ships a ``<script>``
        that builds the DOM at runtime) so we assert the JS markers
        that drive the rendering: the trace-row element class, the
        helper function name, the pill classes referenced by the
        helper, and the JSON-parse + primary-arg extraction in the
        SSE ``tool_call`` branch.
        """
        import re

        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text

        script_match = re.search(r"<script>(.*?)</script>", body, re.DOTALL)
        assert script_match, "chat page must include an inline <script> block"
        script = script_match.group(1)

        # Trace row created by renderMessage (assistant branch). The class
        # assignment is the smoking gun — without it no .chat-trace row
        # would appear in the DOM.
        assert 'className = "chat-trace"' in script, (
            "renderMessage must create a .chat-trace row inside the "
            "assistant message li, before the body"
        )
        # Helper that builds the pill markup and appends to the trace row.
        assert "function addToolPill" in script, (
            "chat.html must define an addToolPill(traceNode, name, primaryArg) "
            "helper for the trace-row rendering"
        )
        # Pill classes referenced by the helper markup.
        assert '"chat-tool-pill"' in script or "'chat-tool-pill'" in script
        assert '"chat-tool-pill__name"' in script or "'chat-tool-pill__name'" in script
        assert '"chat-tool-pill__arg"' in script or "'chat-tool-pill__arg'" in script

        # The tool_call branch MUST parse the JSON data line, find the
        # trace row, and call addToolPill — not create a separate message.
        tool_call_branch_match = re.search(
            r'eventName === "tool_call"(.*?)(?=eventName === |continue;\s*\})',
            script,
            re.DOTALL,
        )
        assert tool_call_branch_match, "SSE parser must have a tool_call branch"
        branch = tool_call_branch_match.group(1)
        assert "JSON.parse(dataLine)" in branch, (
            "tool_call branch must parse the JSON dataLine to extract name/args"
        )
        assert "addToolPill" in branch, (
            "tool_call branch must delegate rendering to addToolPill, "
            "not create a separate transcript li"
        )
        # The old "transcript.insertBefore(toolNode, assistantNode)"
        # approach MUST NOT survive — that's the ugly dead code.
        assert "chat-message--tool" not in branch, (
            "tool_call branch must not create a chat-message--tool li; "
            "the new design renders pills in the assistant's trace row"
        )

    @pytest.mark.asyncio
    async def test_get_chat_clears_input_immediately_on_send(self, chat_router) -> None:
        """The composer MUST clear on send (optimistic UI) — clearing only
        inside the ``sawDone`` branch leaves stale text in the input
        after every error.

        Bug 2 of the work item: input.value="" was only called in the
        successful terminal branch, so on a typed error or network
        drop the user had to manually delete the question they just
        asked.

        Smoke test: assert the HTML contains the input-clearing hook
        OUTSIDE the ``sawDone`` branch — specifically the JS path that
        ``renderMessage("user", text, false);`` immediately follows.
        The exact string ``input.value = ""`` MUST appear at least once
        BEFORE the ``if (sawDone)`` block in the script body, and MUST
        NOT appear inside the success branch (single source of truth).
        """
        import re

        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text

        # Locate the inline script block — the chat page renders the
        # client in a single <script> tag (no external chat.js).
        script_match = re.search(r"<script>(.*?)</script>", body, re.DOTALL)
        assert script_match, "chat page must include an inline <script> block"
        script = script_match.group(1)

        # The hook is called from sendMessage; the user message render
        # is the first place the question text shows up in the DOM.
        render_user_idx = script.index('renderMessage("user", text, false);')
        saw_done_idx = script.index("if (sawDone)")
        saw_done_close_idx = script.index("} else {", saw_done_idx)
        # ``} else {`` opens the error branch — past the success branch.

        # The clear-input hook MUST appear somewhere between the
        # user-message render and the start of the success branch.
        # (Optimistic UI: clear as soon as the user hits Send.)
        clear_idx = script.find('input.value = ""', render_user_idx)
        assert clear_idx != -1, (
            "input-clearing hook missing after renderMessage('user', ...) — "
            "optimistic UI requires immediate clear on send"
        )
        assert clear_idx < saw_done_idx, (
            "input-clearing hook must fire BEFORE the success branch — "
            "the bug was that it only fired inside sawDone, leaving stale "
            "text in the input after errors"
        )

        # And the success branch MUST NOT redundantly clear — single
        # source of truth. The hook lives only in the optimistic path.
        saw_done_block = script[saw_done_idx:saw_done_close_idx]
        assert 'input.value = ""' not in saw_done_block, (
            "input.value = '' must not appear inside the sawDone branch — "
            "the optimistic clear already handled it earlier"
        )

    @pytest.mark.asyncio
    async def test_style_css_defines_trace_row_and_tool_pill_classes(self, chat_router) -> None:
        """``playground/static/style.css`` MUST define the new affordance
        classes the chat client uses to render tool pills:
        ``chat-trace``, ``chat-tool-pill``, ``chat-tool-pill__name``, and
        ``chat-tool-pill__arg``.

        Recruiter-UX gate: without these styles the trace row would
        inherit the default block-flow and the pills would render as
        plain text inside the assistant bubble — no visual distinction
        from the prose, no signal of "this is a tool call". The
        stylesheet defines them so they share the locked Solarized
        Phosphor palette (violet accent for the tool name).
        """
        import re
        from pathlib import Path

        css_path = Path(__file__).resolve().parents[5] / "playground" / "static" / "style.css"
        assert css_path.is_file(), f"style.css not found at {css_path}"
        css = css_path.read_text(encoding="utf-8")

        # Each selector MUST have a non-empty rule body — the test is
        # about the contract "these classes are styled", not just
        # mentioned in comments.
        for selector in (
            ".chat-trace",
            ".chat-tool-pill",
            ".chat-tool-pill__name",
            ".chat-tool-pill__arg",
        ):
            pattern = re.escape(selector) + r"\s*\{[^}]+\}"
            assert re.search(pattern, css), (
                f"style.css must define a non-empty rule for {selector}; "
                f"the pill affordance depends on it being styled"
            )

        # Palette discipline: the tool name MUST use --solar-violet (the
        # audit color and the existing "tool" accent — re-using it keeps
        # the locked palette honest).
        assert ".chat-tool-pill__name" in css and "var(--solar-violet)" in css, (
            ".chat-tool-pill__name must be styled with var(--solar-violet); "
            "the tool name is the visual anchor of the pill"
        )

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
    async def test_get_chat_caps_persisted_history_with_message_and_char_limits(
        self, chat_router
    ) -> None:
        """The inline script MUST cap the persisted history to bound
        localStorage growth AND per-request input-token cost.

        Every ``/chat/stream`` POST sends the FULL ``messages`` array,
        so an unbounded history means unbounded Gemini input tokens on
        every recruiter follow-up. The fix is two module-level caps
        enforced inside ``appendToHistory`` before the write:

          * ``MAX_HISTORY_MESSAGES`` — message-count cap (default 30)
          * ``MAX_HISTORY_CHARS``    — char-count cap on the serialized
                                       JSON (default 8000)

        When EITHER cap is exceeded the script trims from the front
        (FIFO) until both caps are satisfied. The ``appendToHistory``
        function MUST be the single chokepoint that applies both caps
        — the smoke test asserts the JS markers, not the runtime
        behavior (Playwright e2e is the right venue for runtime
        coverage of the trim loop).

        Trimming from the front (oldest messages) is mandatory: a
        recruiter's most recent question is the one they care about
        keeping across a refresh; old context is exactly what they
        want to forget. Silently trimming — no UI banner — is the
        intended UX.
        """
        import re

        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text

        script_match = re.search(r"<script>(.*?)</script>", body, re.DOTALL)
        assert script_match, "chat page must include an inline <script> block"
        script = script_match.group(1)

        # Message-count cap constant MUST be declared at module scope so
        # ``appendToHistory`` can read it.
        assert "MAX_HISTORY_MESSAGES" in script, (
            "chat.html must declare a MAX_HISTORY_MESSAGES constant; "
            "without it, localStorage grows unbounded per session and "
            "every /chat/stream POST ships the entire history as input "
            "tokens to Gemini."
        )

        # Char-count cap constant MUST be declared at module scope.
        assert "MAX_HISTORY_CHARS" in script, (
            "chat.html must declare a MAX_HISTORY_CHARS constant; "
            "message-count alone is not enough — a single huge response "
            "can blow past the count cap in characters and still cost "
            "real money on Gemini's per-token billing."
        )

        # Locate appendToHistory so we can assert the trim loop lives
        # INSIDE it (the cap is enforced on every save, not only on
        # load). We anchor on the signature the existing implementation
        # uses.
        append_match = re.search(
            r"function\s+appendToHistory\s*\([^)]*\)\s*\{(.*?)^\s*\}",
            script,
            re.DOTALL | re.MULTILINE,
        )
        assert append_match, "appendToHistory function not found in chat.html"
        append_body = append_match.group(1)

        # The trim-from-front helper MUST be reachable from appendToHistory
        # — ``messages.shift()`` is the simplest FIFO pop. We assert the
        # call exists inside the function body so it actually runs on
        # every save.
        assert "messages.shift" in append_body, (
            "appendToHistory must call messages.shift() to drop the "
            "oldest message when either cap is exceeded; trimming from "
            "the back would discard the recruiter's most recent question."
        )

        # The guard ``messages.length > 1`` MUST appear alongside the
        # trim loop so a single oversized message can never empty the
        # history completely. This is the silent-fail safe path.
        assert "messages.length > 1" in append_body, (
            "appendToHistory's trim loop must guard with messages.length > 1; "
            "without the guard a single >8000-char assistant reply would "
            "delete the entire transcript and break refresh-restore."
        )

        # The cap constants MUST be referenced from the trim condition
        # itself — declaring them at the top and never using them is the
        # exact failure mode this test guards against.
        assert "MAX_HISTORY_MESSAGES" in append_body, (
            "MAX_HISTORY_MESSAGES must be referenced inside appendToHistory's "
            "trim condition; a declaration-without-use is not a cap."
        )
        assert "MAX_HISTORY_CHARS" in append_body, (
            "MAX_HISTORY_CHARS must be referenced inside appendToHistory's "
            "trim condition; a declaration-without-use is not a cap."
        )

    @pytest.mark.asyncio
    async def test_get_chat_persists_user_message_on_send(self, chat_router) -> None:
        """The inline script MUST call ``appendToHistory("user", ...)``
        inside ``sendMessage`` so user questions survive a refresh.

        Bug 2 of the work item: the user message was rendered into the
        transcript (``renderMessage("user", text, false)``) but NEVER
        persisted. Only the assistant message reached ``appendToHistory``
        (in the ``sawDone`` branch). On a refresh, ``renderHistory`` only
        loaded what was persisted — so the recruiter's question
        vanished, even though the server still had it (it had been
        POSTed to ``/chat/stream``). The fix persists the user message
        BEFORE the request fires, so even an error mid-stream leaves the
        question recoverable on the next page load.
        """
        import re

        app = _make_app(chat_router)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/chat")
        body = response.text

        script_match = re.search(r"<script>(.*?)</script>", body, re.DOTALL)
        assert script_match, "chat page must include an inline <script> block"
        script = script_match.group(1)

        # Anchor on the renderMessage call for the user message — the
        # persist call MUST come AFTER it (so the optimistic UI render
        # still happens, and the persist happens before the fetch
        # fires). Same anchor style as the clear-input test above.
        render_user_idx = script.index('renderMessage("user", text, false);')

        # The persist call MUST appear somewhere between the user
        # message render and the sawDone branch (i.e. it must run as
        # part of the optimistic-UI path, NOT inside the success branch
        # where it would only fire if the stream completed cleanly).
        append_user_idx = script.find(
            'appendToHistory("user", text)', render_user_idx
        )
        assert append_user_idx != -1, (
            "sendMessage must call appendToHistory('user', text) AFTER "
            "renderMessage('user', text, false); otherwise a refresh "
            "during streaming (or on error) loses the recruiter's question. "
            "Only the assistant message was being persisted — bug 2."
        )

        # And it MUST run BEFORE the fetch (the persist-then-request
        # ordering is what guarantees survival on network drops). The
        # ``fetch(STREAM_URL`` call is the obvious boundary.
        fetch_idx = script.find("fetch(STREAM_URL", render_user_idx)
        assert fetch_idx != -1, "sendMessage must issue a fetch to /chat/stream"
        assert append_user_idx < fetch_idx, (
            "appendToHistory('user', text) must run BEFORE the fetch fires; "
            "otherwise a fast failure (network drop, typed error) leaves "
            "no record of the question in localStorage."
        )

        # And it MUST NOT live inside the sawDone branch — that would
        # only persist on success, which is the original bug. We use
        # the same index-based scoping as
        # ``test_get_chat_clears_input_immediately_on_send``.
        saw_done_idx = script.find("if (sawDone)")
        assert saw_done_idx != -1, "sendMessage must have a sawDone branch"
        saw_done_close_idx = script.find("} else {", saw_done_idx)
        assert saw_done_close_idx != -1, "sawDone branch must have a sibling else"
        saw_done_block = script[saw_done_idx:saw_done_close_idx]
        assert 'appendToHistory("user", text)' not in saw_done_block, (
            "appendToHistory('user', text) must NOT live inside the sawDone "
            "success branch — the original bug only persisted on success; "
            "the fix persists optimistically BEFORE the request fires."
        )

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

        This is the contract the browser client relies on to distinguish a
        known agent error from a dropped connection.
        """
        from mcp_server.application.use_cases.ask_portfolio import (
            AskPortfolioUseCase,
        )

        class _ErrorStubAskPortfolio(AskPortfolioUseCase):
            async def astream(
                self, request: AskPortfolioRequest
            ) -> AsyncIterator[AskPortfolioChunk]:
                yield AskPortfolioChunk(kind="token", answer_token="partial ")
                yield AskPortfolioChunk(kind="error", error="rate limit hit")

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
        # sentinel and its follow-up message event.
        assert "data: [ERROR]\n\n" in body
        assert "event: error" in body
        error_data_lines = [
            line for line in body.splitlines() if line.startswith("data: ") and '"message"' in line
        ]
        assert error_data_lines, f"error message event missing; body={body!r}"
        assert json.loads(error_data_lines[0].split("data: ", 1)[1]) == {
            "message": "rate limit hit"
        }
        assert body.index("data: [ERROR]") < body.index("event: error")
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
    async def test_post_chat_stream_enriches_rate_limit_exception(self, chat_router) -> None:
        from mcp_server.application.use_cases.ask_portfolio import AskPortfolioUseCase
        from mcp_server.domain.exceptions import RateLimitExceeded

        class _RateLimitStubAskPortfolio(AskPortfolioUseCase):
            async def astream(
                self, request: AskPortfolioRequest
            ) -> AsyncIterator[AskPortfolioChunk]:
                if False:
                    yield AskPortfolioChunk(kind="done")
                raise RateLimitExceeded("rate limit exceeded for client_ip=127.0.0.1")

        app = _make_app(chat_router)
        app.state.composition.ask_portfolio_use_case = _RateLimitStubAskPortfolio(  # type: ignore[assignment]
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
                    "conversation_id": "rate-limit",
                },
            )

        body = response.text
        assert "data: [ERROR]\n\n" in body
        error_data_lines = [
            line for line in body.splitlines() if line.startswith("data: ") and '"message"' in line
        ]
        assert error_data_lines, f"rate-limit message event missing; body={body!r}"
        assert json.loads(error_data_lines[0].split("data: ", 1)[1]) == {
            "message": "Rate limit exceeded — try again in a minute."
        }
        assert "client_ip" not in body

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
