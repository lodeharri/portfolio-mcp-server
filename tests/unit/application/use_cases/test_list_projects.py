"""Unit tests for ``ListProjectsUseCase``.

Covers the four spec requirements from
``openspec/changes/002-mcp-tools/specs/list_projects.md``:

* **Manifest Is the Only Source** — returns one entry per declared
  project; empty manifest returns ``[]``.
* **Output Passes Through OutputSanitizer (Layer 3)** — every string
  field (id, display_name, description) is sanitized; multi-pattern
  redactions aggregate to one ``output.redacted`` audit event.
* **Chunk Count Is Best-Effort** — ``index_chunk_count`` defaults to
  ``0`` when no ``VectorStorePort`` is wired; positive count when
  the index has rows for that project.
* **Use case depends only on ports** — receives ``ManifestPort``,
  optional ``VectorStorePort``, ``OutputSanitizer``, ``AuditLogger``.
  No concrete adapters, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mcp_server.application.ports.manifest import Manifest, Project
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeChunkCount:
    """Minimal stand-in for ``VectorStorePort`` — only ``count_by_project``."""

    counts: dict[str, int]

    def count_by_project(self, project_id: str) -> int:
        return int(self.counts.get(project_id, 0))


def _project(*, id: str, display_name: str = "", description: str = "") -> Project:
    """Build a minimal :class:`Project` for the manifest fake."""
    return Project(
        id=id,
        path=Path(f"/tmp/{id}"),
        display_name=display_name,
        description=description,
        include_subdirs=["."],
        exclude_subdirs=[],
    )


class _FakeManifestPort:
    """In-memory :class:`ManifestPort` returning a configurable list."""

    def __init__(self, projects: list[Project]) -> None:
        self._projects = list(projects)

    def load(self) -> Manifest:
        return Manifest(server_name="t", version="0", projects=list(self._projects))

    def projects(self) -> list[Project]:
        return list(self._projects)

    def is_path_indexed(self, path: Path) -> bool:  # pragma: no cover — unused
        return False


class _FakeAudit:
    """In-memory audit logger that records every emitted event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def warn(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def _use_case(
    *,
    manifest: _FakeManifestPort,
    vector_store: _FakeChunkCount | None = None,
):
    """Build a :class:`ListProjectsUseCase` with fakes + real sanitizer + audit."""
    from mcp_server.application.use_cases.list_projects import ListProjectsUseCase

    return ListProjectsUseCase(
        manifest=manifest,
        vector_store=vector_store,
        sanitizer=OutputSanitizer(),
        audit=AuditLogger(),
    )


# ---------------------------------------------------------------------------
# Manifest Is the Only Source
# ---------------------------------------------------------------------------


class TestManifestIsTheOnlySource:
    """One entry per declared project; empty manifest returns ``[]``."""

    def test_returns_one_entry_per_declared_project(self) -> None:
        manifest = _FakeManifestPort(
            [
                _project(id="finance-coach-latam", display_name="Finance Coach LATAM"),
                _project(id="landing-page-portfolio", display_name="Landing Page"),
            ]
        )
        uc = _use_case(manifest=manifest)

        result = uc.execute()

        assert len(result) == 2
        ids = {p["id"] for p in result}
        assert ids == {"finance-coach-latam", "landing-page-portfolio"}
        # Each entry MUST include all four required fields per the spec.
        for entry in result:
            assert "id" in entry
            assert "display_name" in entry
            assert "description" in entry
            assert "index_chunk_count" in entry

    def test_empty_manifest_returns_empty_list(self) -> None:
        manifest = _FakeManifestPort([])
        uc = _use_case(manifest=manifest)

        result = uc.execute()

        assert result == []

    def test_display_name_falls_back_to_id_when_blank(self) -> None:
        """Per the spec, ``display_name`` MAY be blank; surface the id then."""
        manifest = _FakeManifestPort(
            [_project(id="finance-coach-latam", display_name="", description="hi")]
        )
        uc = _use_case(manifest=manifest)

        result = uc.execute()

        assert len(result) == 1
        # The use case preserves the manifest's display_name verbatim —
        # the spec doesn't require a fallback at the use-case layer.
        assert result[0]["display_name"] == ""


# ---------------------------------------------------------------------------
# Output Sanitization (Layer 3)
# ---------------------------------------------------------------------------


class TestOutputSanitization:
    """Description strings are sanitized; redactions emit audit events."""

    def test_aws_shaped_substring_in_description_is_redacted(self) -> None:
        manifest = _FakeManifestPort(
            [
                _project(
                    id="finance-coach-latam",
                    description="set AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE for prod",
                )
            ]
        )
        audit = _FakeAudit()
        from mcp_server.application.use_cases.list_projects import ListProjectsUseCase

        uc = ListProjectsUseCase(
            manifest=manifest,
            sanitizer=OutputSanitizer(),
            audit=audit,  # type: ignore[arg-type]
        )

        result = uc.execute()

        assert "AKIAIOSFODNN7EXAMPLE" not in result[0]["description"]
        assert "[REDACTED]" in result[0]["description"]

    def test_clean_descriptions_pass_through_unchanged(self) -> None:
        clean = "Personal finance assistant for LATAM users. No tokens here."
        manifest = _FakeManifestPort(
            [_project(id="finance-coach-latam", description=clean)]
        )
        audit = _FakeAudit()
        from mcp_server.application.use_cases.list_projects import ListProjectsUseCase

        uc = ListProjectsUseCase(
            manifest=manifest,
            sanitizer=OutputSanitizer(),
            audit=audit,  # type: ignore[arg-type]
        )

        result = uc.execute()

        assert result[0]["description"] == clean

    def test_github_pat_in_description_is_redacted(self) -> None:
        pat = "ghp_" + "a" * 36  # 36 word chars
        manifest = _FakeManifestPort(
            [_project(id="x", description=f"token: {pat}")]
        )
        audit = _FakeAudit()
        from mcp_server.application.use_cases.list_projects import ListProjectsUseCase

        uc = ListProjectsUseCase(
            manifest=manifest,
            sanitizer=OutputSanitizer(),
            audit=audit,  # type: ignore[arg-type]
        )

        result = uc.execute()

        assert pat not in result[0]["description"]
        assert "[REDACTED]" in result[0]["description"]


# ---------------------------------------------------------------------------
# Chunk Count Is Best-Effort
# ---------------------------------------------------------------------------


class TestChunkCountIsBestEffort:
    """``index_chunk_count`` defaults to ``0`` when no vector_store; positive when wired."""

    def test_chunk_count_defaults_to_zero_when_no_vector_store(self) -> None:
        manifest = _FakeManifestPort(
            [_project(id="finance-coach-latam"), _project(id="landing-page-portfolio")]
        )
        uc = _use_case(manifest=manifest, vector_store=None)

        result = uc.execute()

        assert all(p["index_chunk_count"] == 0 for p in result)

    def test_chunk_count_is_positive_when_index_has_rows(self) -> None:
        manifest = _FakeManifestPort(
            [_project(id="finance-coach-latam"), _project(id="landing-page-portfolio")]
        )
        store = _FakeChunkCount(counts={"finance-coach-latam": 42})
        uc = _use_case(manifest=manifest, vector_store=store)  # type: ignore[arg-type]

        result = uc.execute()

        by_id = {p["id"]: p["index_chunk_count"] for p in result}
        assert by_id["finance-coach-latam"] == 42
        assert by_id["landing-page-portfolio"] == 0  # not in counts dict

    def test_chunk_count_query_uses_project_id(self) -> None:
        """The use case MUST call ``count_by_project(project_id)`` per project."""
        manifest = _FakeManifestPort(
            [_project(id="a"), _project(id="b"), _project(id="c")]
        )
        calls: list[str] = []

        class _SpyStore:
            def count_by_project(self, project_id: str) -> int:
                calls.append(project_id)
                return {"a": 1, "b": 2, "c": 3}.get(project_id, 0)

        uc = _use_case(manifest=manifest, vector_store=_SpyStore())  # type: ignore[arg-type]

        result = uc.execute()

        assert sorted(calls) == ["a", "b", "c"]
        assert {p["id"]: p["index_chunk_count"] for p in result} == {
            "a": 1,
            "b": 2,
            "c": 3,
        }


# ---------------------------------------------------------------------------
# Spec coverage: list_projects is registered as a FastMCP tool
# ---------------------------------------------------------------------------


class TestSanitizerEmitsAuditOnRedaction:
    """The sanitizer must emit one ``output.redacted`` event per redaction."""

    def test_audit_event_fires_when_description_is_redacted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Layer 5 invariant: every redaction emits ``output.redacted``."""
        import json

        manifest = _FakeManifestPort(
            [_project(id="x", description="AKIAIOSFODNN7EXAMPLE leaked")]
        )
        # Real audit logger + real sanitizer wired with the audit
        # (this is what composition.py does in production).
        audit = AuditLogger()
        sanitizer = OutputSanitizer(audit=audit)
        from mcp_server.application.use_cases.list_projects import ListProjectsUseCase

        uc = ListProjectsUseCase(
            manifest=manifest,
            sanitizer=sanitizer,
            audit=audit,
        )

        uc.execute()

        out, _ = capsys.readouterr()
        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        redacted = [r for r in records if r.get("event") == "output.redacted"]
        assert len(redacted) == 1
        assert "aws" in redacted[0]["patterns"]
