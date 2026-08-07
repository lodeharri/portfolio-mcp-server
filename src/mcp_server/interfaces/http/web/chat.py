"""``/chat`` and ``/chat/stream`` — streaming chat UI surface (PR2b).

Mounted via :func:`build_chat_router` from ``interfaces/http/web/router.py``.
Two routes:

* ``GET /chat`` — renders ``playground/templates/chat.html`` with the
  inline browser client (localStorage persistence + ``fetch`` +
  ``ReadableStream`` SSE parsing).
* ``POST /chat/stream`` — accepts a JSON body of the form
  ``{"messages": [{"role": "user", "content": "..."}, ...],
  "conversation_id": "..."}``, invokes
  :class:`mcp_server.application.use_cases.ask_portfolio.AskPortfolioUseCase.astream`
  on the wired composition, and wraps each chunk as one Server-Sent
  Event::

      AskPortfolioChunk(kind="token")     -> ServerSentEvent(raw_data=<token>)
      AskPortfolioChunk(kind="tool_call") -> ServerSentEvent(data=<dict>, event="tool_call")
      AskPortfolioChunk(kind="done")      -> ServerSentEvent(raw_data="[DONE]")  (terminal)
      AskPortfolioChunk(kind="error")     -> ServerSentEvent(raw_data="[ERROR]")
                                               + typed JSON error event (terminal)

The terminal ``[DONE]`` and ``[ERROR]`` sentinels are consumed by the
browser client (per the ``chat-persistence`` spec): ``[DONE]`` triggers
localStorage history append; ``[ERROR]`` is followed by a typed error event
when the server knows the cause, while a missing follow-up indicates a
connection drop. Errors do NOT persist a partial assistant message.


Per ADR-004 the server is stateless — no DB row, no in-memory
session map, no cookie is set on any response. The browser owns the
``localStorage`` session UUID and sends the full ``messages`` array on
every request.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.sse import ServerSentEvent
from pydantic import BaseModel, Field

from mcp_server.application.use_cases.ask_portfolio import (
    AskPortfolioRequest,
    AskPortfolioUseCase,
)
from mcp_server.domain.exceptions import RateLimitExceeded
from mcp_server.interfaces.http.web.deps import get_composition
from mcp_server.interfaces.http.web.templates import templates

__all__ = ["build_chat_router", "chat_page", "chat_stream_events"]


# ---------------------------------------------------------------------------
# GET /chat — render chat.html
# ---------------------------------------------------------------------------


async def chat_page(request: Request) -> HTMLResponse:
    """Render ``playground/templates/chat.html``.

    The template extends ``base.html`` and embeds the chat client JS
    inline (per the PR2b MVP scope — no separate ``chat.js`` static
    file). The shared :class:`Jinja2Templates` environment keeps
    navigation + vendored HTMX consistent with the rest of the web UI.
    """
    return templates.TemplateResponse(
        request=request,
        name="chat.html",
        context={},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# POST /chat/stream — request body model + SSE streaming
# ---------------------------------------------------------------------------


class _ChatStreamRequest(BaseModel):
    """The shape the browser client POSTs to ``/chat/stream``.

    Per the ``chat-persistence`` spec, the server NEVER stores the
    conversation history; the browser owns it in ``localStorage`` and
    sends the entire ``messages`` array on every request. ``conversation_id``
    is the client's localStorage session UUID (the server never uses
    it for any session-shaping logic — it merely echoes it through
    to the agent's ``AskPortfolioResult.conversation_id``).
    """

    messages: list[dict[str, Any]] = Field(default_factory=list)
    conversation_id: str | None = None


def _client_ip_from(request: Request) -> str:
    """Extract the client IP, falling back to a documented loopback for tests.

    Tests with ``httpx.ASGITransport`` typically leave ``request.client``
    populated with a synthetic ``("127.0.0.1", 50000)`` tuple. When the
    scope doesn't carry a client (e.g. some websocket-style scopes),
    fall back to ``"127.0.0.1"`` so the rate limiter still has a key.
    """
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def _extract_current_question(messages: list[dict[str, Any]]) -> str:
    """Return the trailing user message — the question the agent answers."""
    for entry in reversed(messages):
        if isinstance(entry, dict) and entry.get("role") == "user":
            content = entry.get("content", "")
            return content if isinstance(content, str) else str(content)
    return ""


async def chat_stream_events(
    request: Request,
    payload: Annotated[_ChatStreamRequest, Body(...)],
) -> AsyncIterator[ServerSentEvent]:
    """Yield one ``ServerSentEvent`` per ``AskPortfolioChunk``.

    FastAPI's SSE routing branch (triggered by
    ``response_class=EventSourceResponse`` on the route) treats the
    path-operation function as an async generator when
    ``EventSourceResponse`` is the configured response class — it
    iterates the function's ``__aiter__`` and serializes each item
    via :func:`fastapi.sse.format_sse_event`. So the route handler
    MUST be an async generator itself (no plain ``return``).

    The JSON body is declared as a Pydantic parameter (``Body(...)``)
    rather than read via ``request.json()`` inside the generator —
    FastAPI's SSE branch reads the body once BEFORE invoking the
    generator, so reading it from inside the generator would race
    against the SSE send channel and hang on the receive side.

    Mapping per agent-streaming spec:

    * ``kind="token"``     → ``ServerSentEvent(raw_data=<token>)``
    * ``kind="tool_call"`` → ``ServerSentEvent(data=<dict>, event="tool_call")``
    * ``kind="done"``      → ``ServerSentEvent(raw_data="[DONE]")`` (terminal)
    * ``kind="error"``     → ``ServerSentEvent(raw_data="[ERROR]")`` followed
      by ``ServerSentEvent(data={"message": ...}, event="error")``
      (terminal, REL-3)

    The route is deliberately stateless: no cookie is set, no
    session map is updated, no DB row is created. Per ADR-004 the
    browser owns the conversation in ``localStorage``.
    """
    composition = get_composition(request)
    if composition is None:
        raise HTTPException(status_code=500, detail="composition not wired")
    use_case: AskPortfolioUseCase | None = getattr(composition, "ask_portfolio_use_case", None)
    if use_case is None:
        raise HTTPException(status_code=500, detail="ask_portfolio_use_case not wired")

    client_ip = _client_ip_from(request)
    ask_request = AskPortfolioRequest(
        question=_extract_current_question(payload.messages),
        conversation_id=payload.conversation_id,
        client_ip=client_ip,
    )

    # The first iteration of ``astream`` performs the rate-limit
    # gate. If the limiter blocks, ``RateLimitExceeded`` propagates
    # synchronously (before any yield). Translate it into a terminal
    # ``[ERROR]`` event so the browser client renders the inline
    # affordance rather than seeing a non-SSE 4xx body.
    try:
        async for chunk in use_case.astream(ask_request):
            if chunk.kind == "token":
                yield ServerSentEvent(raw_data=chunk.answer_token or "")
            elif chunk.kind == "tool_call":
                tool_payload = chunk.tool_call if isinstance(chunk.tool_call, dict) else {}
                yield ServerSentEvent(data=tool_payload, event="tool_call")
            elif chunk.kind == "done":
                yield ServerSentEvent(raw_data="[DONE]")
                return
            elif chunk.kind == "error":
                error_message = chunk.error or "The agent could not complete the request."
                yield ServerSentEvent(raw_data="[ERROR]")
                yield ServerSentEvent(
                    data={"message": error_message},
                    event="error",
                )
                return
    except RateLimitExceeded:
        yield ServerSentEvent(raw_data="[ERROR]")
        yield ServerSentEvent(
            data={"message": "Rate limit exceeded — try again in a minute."},
            event="error",
        )
        return


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_chat_router() -> APIRouter:
    """Build the standalone chat router (GET /chat + POST /chat/stream).

    The router is mounted from ``interfaces/http/web/router.py`` via
    ``include_router(build_chat_router())``. Returning a separate
    router (rather than attaching to ``build_web_router``'s own
    ``APIRouter``) keeps the chat surface self-contained and lets the
    unit tests construct a fresh mini-app without dragging the
    landing-page + ``/mcp-ui`` explorer surface along.

    The POST handler is registered with
    ``response_class=ServerSentEvent`` so FastAPI's routing layer
    takes the SSE branch and treats the path-operation function as an
    async generator of ``ServerSentEvent`` instances (per the SSE wire
    contract documented in :func:`chat_stream_events`).
    """
    from fastapi.sse import EventSourceResponse

    router = APIRouter()

    @router.get("/chat", response_class=HTMLResponse, name="chat_page")
    async def _chat_page(request: Request) -> HTMLResponse:
        return await chat_page(request)

    @router.post(
        "/chat/stream",
        response_class=EventSourceResponse,
        name="chat_stream",
    )
    async def _chat_stream(
        request: Request,
        payload: _ChatStreamRequest,  # type: ignore[valid-type]
    ) -> AsyncIterator[ServerSentEvent]:
        async for event in chat_stream_events(request, payload):
            yield event

    return router


#: Convenient ``json.dumps`` default for SSE ``data:`` payloads that
#: must avoid non-ASCII surprises in old proxies (the browser fetches
#: the stream as UTF-8 — ``ensure_ascii=False`` keeps human text legible
#: in dev tools).
_JSON_KW: dict[str, Any] = {"ensure_ascii": False}


def _json_payload(payload: dict[str, Any]) -> str:
    """Serialize a tool-call payload the same way ``format_sse_event`` expects."""
    return json.dumps(payload, **_JSON_KW)
