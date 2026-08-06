"""Request-scoped helpers for the web router.

Each route looks up the wired :class:`mcp_server.composition.Composition`
from ``request.app.state.composition`` (the composition root is the
single wiring point per ADR-001). The helper centralizes the lookup so
the route handlers stay focused on use-case invocation and rendering.

Hexagonal contract: this module imports application use cases ONLY —
never ``infrastructure/`` adapters. The composition root owns the
adapter wiring.
"""

from __future__ import annotations

from fastapi import Request, status
from fastapi.responses import JSONResponse

from mcp_server.composition import Composition

__all__ = ["composition_not_wired", "get_composition"]


def get_composition(request: Request) -> Composition | None:
    """Return the wired :class:`Composition` attached by ``create_app()``.

    Returns ``None`` if the app was built without a composition root
    (e.g. a slim test fixture). Routes that require composition MUST
    check the return value and call :func:`composition_not_wired` to
    produce a uniform 500 response.
    """
    return getattr(request.app.state, "composition", None)


def composition_not_wired(request: Request) -> JSONResponse:
    """Return a 500 JSONResponse — the composition root is missing.

    Used by routes that require a wired composition. Kept as a
    helper so the same response shape is emitted across the
    playground routes.
    """
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "composition root not wired in app.state"},
    )
