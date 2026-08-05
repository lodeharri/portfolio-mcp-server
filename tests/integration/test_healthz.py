"""Integration tests for ``GET /healthz``.

These tests are RED until ``src/mcp_server/interfaces/http/healthz.py`` and
the wiring in ``src/mcp_server/app.py`` are implemented.
"""

from __future__ import annotations

import httpx

from mcp_server.app import create_app


def _make_client() -> httpx.AsyncClient:
    """Build an async httpx client wired to ``create_app()`` via ASGI transport."""
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


class TestHealthzEndpoint:
    async def test_healthz_returns_200(self) -> None:
        async with _make_client() as client:
            response = await client.get("/healthz")
        assert response.status_code == 200

    async def test_healthz_returns_version_payload(self) -> None:
        async with _make_client() as client:
            response = await client.get("/healthz")
        data = response.json()
        # Spec from openspec/changes/001-bootstrap/specs/app-bootstrap.md:
        # body MUST contain status, version, commit_sha, built_at
        assert data["status"] == "ok"
        assert "version" in data
        assert "commit_sha" in data
        assert "built_at" in data

    async def test_healthz_payload_values_are_strings(self) -> None:
        async with _make_client() as client:
            response = await client.get("/healthz")
        data = response.json()
        assert isinstance(data["version"], str)
        assert isinstance(data["commit_sha"], str)
        assert isinstance(data["built_at"], str)

    async def test_healthz_does_not_404(self) -> None:
        """Sanity: /healthz is registered (returns 200, not 404)."""
        async with _make_client() as client:
            response = await client.get("/healthz")
        assert response.status_code != 404
        assert response.status_code != 405
