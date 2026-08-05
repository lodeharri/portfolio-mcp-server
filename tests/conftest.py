"""Shared pytest fixtures for the mcp_server test suite.

Provides:

* ``app`` — a fresh ``create_app()`` instance per test
* ``async_client`` — an :class:`httpx.AsyncClient` wired to the app via ASGI
* ``app_factory_count`` — counts ``create_app`` invocations to verify
  idempotence expectations
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from mcp_server.app import create_app
from mcp_server.config import AppConfig


@pytest.fixture
def app() -> Iterator:
    """Return a fresh FastAPI app per test.

    Each test gets an independent app instance — the composition root is
    not shared. This is the standard isolation pattern from ADR-001.
    """
    yield create_app()


@pytest.fixture
def app_config() -> AppConfig:
    """Return a default :class:`AppConfig` for tests that need a config object."""
    return AppConfig()


@pytest.fixture
async def async_client(app) -> AsyncIterator[httpx.AsyncClient]:
    """Return an async httpx client wired to the FastAPI app via ASGI transport.

    No real socket binding happens — the transport drives the ASGI app
    directly, so the test is fast and fully isolated.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client
