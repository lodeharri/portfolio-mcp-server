"""Conformance tests for ``src/mcp_server/application/ports/rate_limiter.py``.

The :class:`RateLimiterPort` Protocol declares the contract a slowapi-backed
rate limiter must satisfy, per the orchestrator's PR2 spec:

* ``check(client_ip: str) -> bool`` — returns ``True`` if request is allowed,
  ``False`` if rate-limited.
* ``limit() -> str`` — returns the configured limit string (e.g.
  ``"30/minute"``).

Used by both the FastAPI middleware (HTTP boundary) and any future
MCP-level rate limiter. Layer 5 of the 5-layer security model.
"""

from __future__ import annotations

import inspect


class TestRateLimiterPortProtocol:
    """``RateLimiterPort`` declares the contract for in-memory rate limiting."""

    def test_rate_limiter_port_protocol_exists(self) -> None:
        from mcp_server.application.ports.rate_limiter import RateLimiterPort

        assert RateLimiterPort is not None

    def test_rate_limiter_port_has_check(self) -> None:
        from mcp_server.application.ports.rate_limiter import RateLimiterPort

        members = dict(inspect.getmembers(RateLimiterPort))
        assert "check" in members

    def test_rate_limiter_port_has_limit(self) -> None:
        from mcp_server.application.ports.rate_limiter import RateLimiterPort

        members = dict(inspect.getmembers(RateLimiterPort))
        assert "limit" in members


class TestRateLimiterPortConformance:
    """A class with the right methods satisfies ``RateLimiterPort``."""

    def test_fake_rate_limiter_satisfies_protocol(self) -> None:
        from mcp_server.application.ports.rate_limiter import RateLimiterPort

        class FakeRateLimiter:
            def check(self, client_ip: str) -> bool:
                return True

            def limit(self) -> str:
                return "30/minute"

        assert isinstance(FakeRateLimiter(), RateLimiterPort)

    def test_fake_rate_limiter_returns_limit_string(self) -> None:
        from mcp_server.application.ports.rate_limiter import RateLimiterPort

        class FakeRateLimiter:
            def check(self, client_ip: str) -> bool:
                return client_ip != "blocked"

            def limit(self) -> str:
                return "30/minute"

        fake = FakeRateLimiter()
        assert fake.limit() == "30/minute"
        assert fake.check("127.0.0.1") is True
        assert fake.check("blocked") is False

    def test_class_without_methods_does_not_satisfy_protocol(self) -> None:
        from mcp_server.application.ports.rate_limiter import RateLimiterPort

        class NotARateLimiter:
            pass

        assert not isinstance(NotARateLimiter(), RateLimiterPort)