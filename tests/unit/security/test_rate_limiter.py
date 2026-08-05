"""Tests for ``src/mcp_server/security/rate_limiter.py``.

Layer 5 of the 5-layer security model. The
:class:`SlowapiRateLimiter` wraps ``slowapi.Limiter`` and exposes the
:class:`RateLimiterPort` contract:

* ``check(client_ip)`` — returns ``True`` if under the limit, ``False``
  if rate-limited.
* ``limit()`` — returns the slowapi limit string (``"30/minute"`` by
  default).

Tests are RED until the adapter exists. They use slowapi's in-memory
backend (no Redis), which is the production target for the
single-process uvicorn deployment (``--workers 1`` per ADR-001).
"""

from __future__ import annotations


class TestSlowapiRateLimiterCheck:
    """``check(client_ip)`` returns ``True`` for under-limit, ``False`` otherwise."""

    def test_first_request_returns_true(self) -> None:
        from mcp_server.security.rate_limiter import SlowapiRateLimiter

        limiter = SlowapiRateLimiter(limit="30/minute")
        assert limiter.check("127.0.0.1") is True

    def test_limit_property_returns_configured_string(self) -> None:
        from mcp_server.security.rate_limiter import SlowapiRateLimiter

        limiter = SlowapiRateLimiter(limit="30/minute")
        assert limiter.limit() == "30/minute"

    def test_custom_limit_string_is_used(self) -> None:
        from mcp_server.security.rate_limiter import SlowapiRateLimiter

        limiter = SlowapiRateLimiter(limit="5/minute")
        assert limiter.limit() == "5/minute"

    def test_31st_request_returns_false(self) -> None:
        """Send 30 allowed requests, then the 31st MUST be denied.

        This exercises slowapi's per-IP sliding window directly so the
        behaviour is testable without an HTTP boundary.
        """
        from mcp_server.security.rate_limiter import SlowapiRateLimiter

        limiter = SlowapiRateLimiter(limit="30/minute")
        client_ip = "10.0.0.42"

        # First 30 calls all return True.
        for i in range(30):
            assert limiter.check(client_ip) is True, f"call {i + 1} should be allowed"

        # 31st call returns False.
        assert limiter.check(client_ip) is False


class TestSlowapiRateLimiterPortConformance:
    """``SlowapiRateLimiter`` satisfies ``RateLimiterPort`` structurally."""

    def test_satisfies_rate_limiter_port(self) -> None:
        from mcp_server.application.ports.rate_limiter import RateLimiterPort
        from mcp_server.security.rate_limiter import SlowapiRateLimiter

        limiter = SlowapiRateLimiter(limit="30/minute")
        assert isinstance(limiter, RateLimiterPort)


class TestSlowapiRateLimiterIsolation:
    """Each IP has its own sliding window."""

    def test_different_ips_have_independent_windows(self) -> None:
        from mcp_server.security.rate_limiter import SlowapiRateLimiter

        limiter = SlowapiRateLimiter(limit="2/minute")

        # IP A: 2 requests allowed, 3rd blocked.
        assert limiter.check("1.1.1.1") is True
        assert limiter.check("1.1.1.1") is True
        assert limiter.check("1.1.1.1") is False

        # IP B: independent window, still allowed.
        assert limiter.check("2.2.2.2") is True
        assert limiter.check("2.2.2.2") is True
        assert limiter.check("2.2.2.2") is False


class TestSlowapiRateLimiterAuditIntegration:
    """The limiter emits an audit event when ``audit=`` is provided."""

    def test_exceeding_limit_calls_audit_warn(self) -> None:
        from mcp_server.security.rate_limiter import SlowapiRateLimiter

        captured: list[tuple[str, dict]] = []

        class _FakeAudit:
            def warn(self, event: str, **fields: object) -> None:
                captured.append((event, dict(fields)))

        limiter = SlowapiRateLimiter(limit="1/minute", audit=_FakeAudit())
        assert limiter.check("9.9.9.9") is True
        # Second call hits the limit → audit warn is fired.
        assert limiter.check("9.9.9.9") is False
        assert any(event == "rate_limit.exceeded" for event, _ in captured)
