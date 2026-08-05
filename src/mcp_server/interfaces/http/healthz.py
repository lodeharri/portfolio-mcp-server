"""``GET /healthz`` route handler.

Returns the operational probe payload required by
``openspec/changes/001-bootstrap/specs/app-bootstrap.md``::

    {
      "status": "ok",
      "version": "<package version>",
      "commit_sha": "<git sha or 'dev'>",
      "built_at": "<ISO-8601 or 'now'>"
    }

All values come from the typed :class:`AppConfig` attached to
``app.state.composition.config``. The handler does NOT read ``os.environ``
directly — that is :mod:`mcp_server.config`'s job.
"""

from __future__ import annotations

from fastapi import APIRouter, Request


def build_healthz_router() -> APIRouter:
    """Build the ``/healthz`` router.

    The router is built lazily so each ``create_app()`` call gets its own
    router bound to that app's composition state. Mounting happens in
    :mod:`mcp_server.app`.
    """
    router = APIRouter()

    @router.get("/healthz")
    async def healthz(request: Request) -> dict[str, str]:
        """Return the operational probe payload."""
        composition = request.app.state.composition
        build_info = composition.config.build_info
        return {
            "status": "ok",
            "version": build_info.version,
            "commit_sha": build_info.commit_sha,
            "built_at": build_info.built_at,
        }

    return router


__all__ = ["build_healthz_router"]
