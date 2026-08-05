"""Rate limiter port — application-layer contract for in-memory rate limiting.

Layer 5 of the 5-layer security model. The port is decoupled from
slowapi so the application layer (use cases, MCP tools) can call
``check(client_ip)`` without depending on the HTTP boundary's library.

The composition root wires :class:`SlowapiRateLimiter` (which lives in
``src/mcp_server/security/rate_limiter.py``) into the container. The
adapter delegates to slowapi's in-memory backend — the Dockerfile MUST
start uvicorn with ``--workers 1`` so the in-memory state stays
consistent across requests (see ADR-001 follow-up R7).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class RateLimiterPort(Protocol):
    """Contract for any in-memory rate-limiter adapter.

    The ``limit()`` accessor exists so the HTTP middleware can register
    the same limit with slowapi's exception handler without hard-coding
    the string in two places.
    """

    def check(self, client_ip: str) -> bool:
        """Return ``True`` if the request from ``client_ip`` is allowed.

        Args:
            client_ip: Client IP, typically ``request.client.host``
                extracted by the FastAPI middleware.

        Returns:
            ``True`` when the request is under the configured limit,
            ``False`` when the limit has been exceeded.
        """
        ...

    def limit(self) -> str:
        """Return the slowapi limit string (e.g. ``"30/minute"``).

        Used by the HTTP middleware to register slowapi's exception
        handler. The string format follows slowapi's ``<count>/<period>``
        syntax.
        """
        ...


__all__ = ["RateLimiterPort"]