"""``OutputSanitizerMiddleware`` — Layer 3 enforcement at the HTTP boundary.

This middleware wraps every HTTP response and runs the body through
:meth:`OutputSanitizer.sanitize_json` so any secret-shaped values
(AWS, GitHub, OpenAI, Gemini, generic credentials) are replaced with
``[REDACTED]`` before the bytes leave the server.

The middleware is registered in ``create_app()`` per task 2.13. Its
placement is at the response handler layer (i.e. ``add_middleware``)
so the inner route handler runs first and emits its native bytes; the
middleware then rewrites the body before the client sees it.

Skipped routes
--------------

Two route prefixes are excluded from sanitization to keep cost low on
known-safe endpoints: ``/healthz`` (a thin status probe) and
``/mcp`` (the MCP transport whose payload bytes are already governed
by the MCP tool-layer sanitizer on the wire).
"""

from __future__ import annotations

import json
from typing import Iterable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from mcp_server.security.output_sanitizer import OutputSanitizer

__all__ = ["OutputSanitizerMiddleware"]


# Path prefixes the middleware MUST NOT touch. These routes either
# never echo user data (``/healthz``) or are governed by another
# sanitizer layer (``/mcp``).
SKIP_PATH_PREFIXES: tuple[str, ...] = ("/healthz", "/mcp")


class OutputSanitizerMiddleware(BaseHTTPMiddleware):
    """Rewrite every response body through :class:`OutputSanitizer`.

    Construction is fed by ``add_middleware(..., sanitizer=...)`` so
    the middleware class itself stays parameter-free and the
    composition root is the only module that builds the sanitizer.

    The middleware serializes the response body once, runs
    :meth:`OutputSanitizer.sanitize_json` over any JSON-shaped
    payload, and re-encodes the redacted body back. Plain-text bodies
    get :meth:`sanitize` (single-pass regex).

    Audit emission is handled by the injected :class:`OutputSanitizer`
    itself (Layer 5 contract: every redaction logs ``output.redacted``).
    The middleware does NOT emit additional events.
    """

    SKIP_PATH_PREFIXES: tuple[str, ...] = SKIP_PATH_PREFIXES

    def __init__(self, app, *, sanitizer: OutputSanitizer) -> None:
        """Initialize the middleware.

        Args:
            app: The ASGI app this middleware wraps (FastAPI's
                ``add_middleware`` passes this automatically).
            sanitizer: The shared :class:`OutputSanitizer` from the
                composition root.
        """
        super().__init__(app)
        self._sanitizer = sanitizer

    async def dispatch(self, request: Request, call_next) -> Response:
        """Intercept the response, sanitize the body, return the rewritten one.

        The route runs first via ``call_next(request)``. If its
        response body is non-empty AND the path is not in the skip
        list, the body bytes are sanitized and the response is
        reconstructed with the redacted bytes.
        """
        response = await call_next(request)

        if _should_skip(request.url.path, self.SKIP_PATH_PREFIXES):
            return response

        body = b""
        async for chunk in response.body_iterator:
            if isinstance(chunk, str):
                body += chunk.encode("utf-8")
            else:
                body += bytes(chunk)

        if not body:
            return response

        sanitized = self._sanitize_body(body, source=request.url.path)
        # Build a fresh response with the redacted body. Streaming
        # semantics are not needed here — the body is small (tool
        # responses top out at a few KB).
        headers = _filter_hop_headers(dict(response.headers))
        new_response = Response(
            content=sanitized,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
        )
        return new_response

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sanitize_body(self, body: bytes, *, source: str) -> bytes:
        """Run the sanitizer over the body bytes (JSON or text).

        JSON detection is heuristic: starts with ``{`` or ``[`` after
        whitespace. Non-JSON bodies fall back to plain :meth:`sanitize`.
        """
        head = body.lstrip()
        # ``head`` is ``bytes`` — compare against single-byte starts.
        if head.startswith(b"{") or head.startswith(b"["):
            try:
                decoded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                # Malformed JSON: rewrite as plain text.
                return self._sanitize_text(body, source=source)
            result = self._sanitizer.sanitize_json(decoded, source=source)
            # The sanitizer returns compact-JSON redacted_text.
            return result.redacted_text.encode("utf-8")
        return self._sanitize_text(body, source=source)

    def _sanitize_text(self, body: bytes, *, source: str) -> bytes:
        text = body.decode("utf-8", errors="replace")
        result = self._sanitizer.sanitize(text, source=source)
        return result.redacted_text.encode("utf-8")


def _should_skip(path: str, prefixes: Iterable[str]) -> bool:
    """True iff ``path`` starts with any prefix in ``prefixes``."""
    return any(path.startswith(p) for p in prefixes)


# Headers that are hop-by-hop (RFC 7230 §6.1) and must NOT be carried
# over to the rewritten response. The sanitizer middleware only
# preserves a safe subset so the client doesn't see stale
# Content-Length or Transfer-Encoding markers from the pre-sanitize
# response.
_HOP_HEADERS = frozenset(
    {"content-length", "transfer-encoding", "connection", "keep-alive"}
)


def _filter_hop_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop hop-by-hop headers; return a new dict with the rest."""
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_HEADERS}
