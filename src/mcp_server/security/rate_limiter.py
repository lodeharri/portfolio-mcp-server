"""Slowapi-backed rate limiter — implements :class:`RateLimiterPort`.

Layer 5 of the 5-layer security model. Wraps ``slowapi.Limiter`` and
exposes the application-layer contract:

* ``check(client_ip)`` — returns ``True`` if under the limit, ``False``
  otherwise.
* ``limit()`` — returns the slowapi limit string (``"30/minute"``).

The default limit is ``30/minute`` per the design.md (30 req/min/IP).
The composition root wires a single :class:`SlowapiRateLimiter` into
the container; the FastAPI middleware (PR2) calls ``check`` per
request. Because the limiter state is in-memory, uvicorn MUST run with
``--workers 1`` so requests see a consistent view (R7).
"""

from __future__ import annotations

from limits import parse as parse_rate_limit
from slowapi import Limiter
from slowapi.util import get_remote_address

_DEFAULT_LIMIT = "30/minute"


class SlowapiRateLimiter:
    """In-memory rate limiter implementing :class:`RateLimiterPort`.

    Args:
        limit: Slowapi limit string in ``"<count>/<period>"`` syntax
            (e.g. ``"30/minute"``). Default ``"30/minute"``.
        audit: Optional audit logger. When provided, a
            ``rate_limit.exceeded`` event is emitted on every denied
            request.
    """

    def __init__(
        self,
        limit: str = _DEFAULT_LIMIT,
        audit: object | None = None,
    ) -> None:
        self._limit_str = limit
        self._limiter = Limiter(
            key_func=get_remote_address,
            default_limits=[limit],
        )
        # Parse the limit string into a limits-library RateLimitItem once
        # so ``check`` does not re-parse on every call.
        self._item = parse_rate_limit(limit)
        self._audit = audit

    def check(self, client_ip: str) -> bool:
        """Return ``True`` if the request from ``client_ip`` is allowed.

        Args:
            client_ip: Client IP (typically ``request.client.host``).

        Returns:
            ``True`` when under the configured limit, ``False`` when
            the sliding window has been exceeded.
        """
        # slowapi wraps a ``limits.strategies.FixedWindowRateLimiter``;
        # its ``test()`` returns True if a hit would still be allowed,
        # then ``hit()`` actually advances the counter. We do BOTH on a
        # successful check so repeated ``check()`` calls eventually
        # exhaust the window.
        strategy = self._limiter.limiter
        allowed = bool(strategy.test(self._item, "check", client_ip))
        if allowed:
            strategy.hit(self._item, "check", client_ip)
        else:
            self._emit_audit("rate_limit.exceeded", client_ip=client_ip)
        return allowed

    def limit(self) -> str:
        """Return the slowapi limit string (e.g. ``"30/minute"``)."""
        return self._limit_str

    def _emit_audit(self, event: str, **fields: object) -> None:
        """Forward an audit event when a logger was injected at construction."""
        if self._audit is None:
            return
        emit = getattr(self._audit, "warn", None)
        if callable(emit):
            emit(event, **fields)


__all__ = ["SlowapiRateLimiter"]
