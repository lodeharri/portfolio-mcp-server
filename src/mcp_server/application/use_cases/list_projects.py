"""``ListProjectsUseCase`` — application-layer ``list_projects`` MCP tool.

The use case enumerates projects declared in :class:`ManifestPort` and
returns one ``ProjectSummary`` per project, with an optional
``index_chunk_count`` reported by :class:`VectorStorePort` when wired.
The response is sanitized through :class:`OutputSanitizer` (Layer 3)
before returning to the MCP client.

Hexagonal contract
------------------

Depends ONLY on ports:

* :class:`mcp_server.application.ports.manifest.ManifestPort`
* :class:`mcp_server.application.ports.vector_store.VectorStorePort`
  (optional — ``index_chunk_count`` defaults to ``0`` when absent)
* :class:`mcp_server.security.output_sanitizer.OutputSanitizer`
* :class:`mcp_server.security.audit.AuditLogger`

No concrete adapter imports, no LLM, no FastMCP / FastAPI.

Why a dict return (not a Pydantic model)?
-----------------------------------------

The use case returns ``list[dict]`` instead of a dedicated
``ProjectSummary`` entity because the wrapper layer
(``interfaces/mcp/tools.py``) needs JSON-serializable payloads and
the four fields are already primitives (``str`` / ``int``). Adding a
Pydantic entity just to immediately ``model_dump()`` it would be
ceremony without value — the spec defines the four fields and the
shape IS the schema.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from mcp_server.application.ports.manifest import ManifestPort
from mcp_server.application.ports.vector_store import VectorStorePort
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer

__all__ = ["ListProjectsUseCase", "ProjectSummaryDict"]


# Type alias for the per-project dict shape (id / display_name /
# description / index_chunk_count). Kept loose so the use case can
# ``sanitize_json`` the whole list in one pass.
ProjectSummaryDict = dict[str, Any]


@runtime_checkable
class _VectorCountProtocol(Protocol):
    """Structural slice of :class:`VectorStorePort` the use case needs.

    The use case only needs ``count_by_project(project_id) -> int``;
    declaring the narrower protocol keeps unit-test fakes tiny
    (no need to fake ``has_hash`` / ``upsert`` / ``search``).
    """

    def count_by_project(self, project_id: str) -> int: ...


class ListProjectsUseCase:
    """Enumerate declared projects with sanitized metadata + chunk count.

    Args:
        manifest: Adapter exposing :meth:`projects()`. Eagerly the YAML
            adapter; structural Protocol keeps the use case testable
            with simple fakes.
        vector_store: Optional adapter exposing
            :meth:`count_by_project(project_id) -> int`. When ``None``
            (e.g. preindex has not run, or tests skip the vector
            store), every project's ``index_chunk_count`` defaults
            to ``0``.
        sanitizer: :class:`OutputSanitizer` — Layer 3.
        audit: :class:`AuditLogger` — Layer 5. ``output.redacted``
            events are emitted by the sanitizer itself, so the use
            case doesn't need to call ``audit.warn`` directly for
            redactions; the audit is still wired in case future
            changes need to emit per-project events.

    Returns:
        ``list[ProjectSummaryDict]`` — one dict per declared project.
    """

    def __init__(
        self,
        *,
        manifest: ManifestPort,
        vector_store: VectorStorePort | None = None,
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
    ) -> None:
        self.manifest = manifest
        self.vector_store = vector_store
        self.sanitizer = sanitizer
        self.audit = audit

    def execute(self) -> list[ProjectSummaryDict]:
        """Return sanitized project summaries, one per declared project.

        Empty manifest returns ``[]`` (no exception). The full payload
        is sanitized through :meth:`OutputSanitizer.sanitize_json` so
        any token-shaped substring inside ``display_name`` or
        ``description`` is replaced with ``[REDACTED]`` before the
        MCP layer serializes it.
        """
        payload: list[ProjectSummaryDict] = []
        for project in self.manifest.projects():
            chunk_count = self._chunk_count_for(project.id)
            payload.append(
                {
                    "id": project.id,
                    "display_name": project.display_name,
                    "description": project.description,
                    "index_chunk_count": chunk_count,
                }
            )

        # Layer 3: sanitize the whole payload in one pass. ``source``
        # shows up in the audit log so recruiters can see which tool
        # redacted which match.
        sanitized = self.sanitizer.sanitize_json(payload, source="list_projects")
        # ``sanitize_json`` returns ``SanitizedOutput(redacted_text, incidents)``
        # where ``redacted_text`` is the compact-JSON string. Re-parse
        # back to a list so the wrapper can hand a real ``list[dict]``
        # to FastMCP. ``json.loads`` is safe: the sanitizer never
        # injects characters outside the JSON alphabet.
        import json

        return json.loads(sanitized.redacted_text)

    def _chunk_count_for(self, project_id: str) -> int:
        """Return the index chunk count for ``project_id`` (0 if no store)."""
        if self.vector_store is None:
            return 0
        # Structural check — keeps the unit-test fakes simple. The
        # full port's ``count_by_project`` is added in 002-mcp-tools PR1
        # to support this use case.
        if not isinstance(self.vector_store, _VectorCountProtocol):
            return 0
        try:
            return int(self.vector_store.count_by_project(project_id))
        except Exception:  # noqa: BLE001 — defensive default per spec
            return 0
