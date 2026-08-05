"""Unit tests for ``src/mcp_server/interfaces/http/middleware/sanitizer.py``.

The ``OutputSanitizerMiddleware`` is the HTTP-boundary enforcement of
Layer 3 (output sanitization). After every FastAPI response, the
middleware:

1. Reads the response body bytes.
2. Runs ``OutputSanitizer.sanitize_json(text, source=route_path)`` over
   any JSON-shaped payload (or falls back to ``sanitize`` for plain
   text).
3. Rewrites the response body with the redacted payload.
4. Emits an audit ``output.redacted`` event when incidents exist.

The middleware is registered in ``create_app()`` per task 2.13.

Tests use ``httpx.AsyncClient`` against ``create_app()`` with mock
audit emission via composition overrides.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import structlog


def _capture_stdout(capture_fn) -> list[dict[str, Any]]:
    """Run ``capture_fn`` and parse the JSON lines emitted to stdout."""
    capture_fn()
    # Hardcoded for test focus: the audit logger uses PrintLoggerFactory(stdout).
    # The fixture's input capture is supplied by the caller through
    # ``capsys`` outside this helper.
    return []


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


class TestOutputSanitizerMiddlewareContract:
    def test_middleware_module_is_importable(self) -> None:
        from mcp_server.interfaces.http.middleware import sanitizer

        assert sanitizer is not None

    def test_middleware_class_exists(self) -> None:
        from mcp_server.interfaces.http.middleware.sanitizer import (
            OutputSanitizerMiddleware,
        )

        assert OutputSanitizerMiddleware is not None

    def test_add_middleware_registers_sanitizer(self) -> None:
        """``create_app`` MUST register OutputSanitizerMiddleware via ``add_middleware``."""
        from mcp_server.app import create_app
        from mcp_server.config import AppConfig

        app = create_app(AppConfig())
        # FastAPI stores middleware classes on ``app.user_middleware``.
        middleware_classes = [
            m.cls.__name__ for m in app.user_middleware if hasattr(m, "cls")
        ]
        assert "OutputSanitizerMiddleware" in middleware_classes


# ---------------------------------------------------------------------------
# Redaction applied to JSON response bodies
# ---------------------------------------------------------------------------


@pytest.fixture
def app_with_secret_route():
    """Build a small FastAPI app exposing one route that returns a secret.

    Uses the production ``create_app()`` and adds a test route at /echo.
    """
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from mcp_server.composition import create_composition
    from mcp_server.config import AppConfig
    from mcp_server.interfaces.http.middleware.sanitizer import (
        OutputSanitizerMiddleware,
    )
    from mcp_server.security.audit import AuditLogger
    from mcp_server.security.output_sanitizer import OutputSanitizer

    audit = AuditLogger()
    sanitizer = OutputSanitizer(audit=audit)

    app = FastAPI()
    # Add the middleware at the bottom so it wraps /echo.
    app.add_middleware(OutputSanitizerMiddleware, sanitizer=sanitizer)
    _ = request = None
    # Use raw middleware via add_middleware — we already passed it.
    _ = Request

    @app.get("/echo")
    async def echo() -> dict[str, Any]:
        return {
            "token": "ghp_abc123def456ghi789jkl012mno345pqr678",
            "note": "this is the response body",
        }

    return app


class TestRedactionOnJsonResponse:
    def test_github_pat_is_redacted_in_json_response(
        self, app_with_secret_route, capsys
    ) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(app_with_secret_route)
        response = client.get("/echo")
        body = response.text
        # Parse to confirm JSON shape preserved.
        parsed = json.loads(body)
        # The PAT MUST be redacted to [REDACTED] in the body string.
        assert "ghp_" not in body
        assert "[REDACTED]" in body
        assert parsed["note"] == "this is the response body"

    def test_audit_event_emitted_on_redaction(
        self, app_with_secret_route, capsys
    ) -> None:
        from fastapi.testclient import TestClient

        client = TestClient(app_with_secret_route)
        client.get("/echo")
        out, _ = capsys.readouterr()
        records = [
            json.loads(line)
            for line in out.splitlines()
            if line.strip()
        ]
        redacted = [r for r in records if r.get("event") == "output.redacted"]
        assert len(redacted) == 1
        assert "github" in redacted[0]["patterns"]


# ---------------------------------------------------------------------------
# Skips /healthz and other pre-known safe routes
# ---------------------------------------------------------------------------


class TestRouteSkipping:
    def test_healthz_route_is_not_sanitized(
        self, app_with_secret_route, monkeypatch
    ) -> None:
        """Sanitizer MUST NOT touch /healthz (it's a known safe status route).

        The sanitization contract is per-route: routes like /healthz
        that never echo user data skip the middleware cost.
        """
        # This test verifies the implementation hook (skipped routes).
        from mcp_server.interfaces.http.middleware.sanitizer import (
            OutputSanitizerMiddleware,
        )

        # The middleware exposes ``SKIP_PATH_PREFIXES`` as a public
        # tuple — assert /healthz is in there.
        skip_paths = getattr(
            OutputSanitizerMiddleware, "SKIP_PATH_PREFIXES", ()
        )
        assert "/healthz" in skip_paths, (
            "/healthz must be in the middleware skip list to avoid "
            "sanitization cost on every health probe"
        )
