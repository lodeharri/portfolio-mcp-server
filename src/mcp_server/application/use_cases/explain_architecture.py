"""Application use case for explaining a project's architecture from ADRs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcp_server.application.ports.llm import LLMPort
from mcp_server.application.ports.manifest import ManifestPort
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer

MAX_ADR_BYTES = 64 * 1024


@dataclass(frozen=True)
class ExplainArchitectureRequest:
    project_id: str
    max_tokens: int = 500


@dataclass(frozen=True)
class ExplainArchitectureResult:
    project_id: str
    display_name: str
    summary: str
    sources: list[str]


class ExplainArchitectureUseCase:
    def __init__(
        self,
        *,
        manifest: ManifestPort,
        llm: LLMPort,
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
    ) -> None:
        self.manifest = manifest
        self.llm = llm
        self.sanitizer = sanitizer
        self.audit = audit

    def execute(self, request: ExplainArchitectureRequest) -> ExplainArchitectureResult:
        project = self._project(request.project_id)
        if not project.adr_path:
            raise FileNotFoundError(f"ADR path is not declared for project {project.id}")
        adr_file = self._declared_path(project.path, project.adr_path)
        try:
            size = adr_file.stat().st_size
        except FileNotFoundError:
            raise
        text = adr_file.read_text()
        if size > MAX_ADR_BYTES:
            text = text[:MAX_ADR_BYTES]
            self.audit.warn("llm.truncated", source="explain_architecture", bytes=size)
        summary = self.llm.summarize(text, max_tokens=request.max_tokens)
        clean_summary = self.sanitizer.sanitize(
            summary, source="explain_architecture"
        ).redacted_text
        clean_source = self.sanitizer.sanitize(
            str(adr_file), source="explain_architecture"
        ).redacted_text
        return ExplainArchitectureResult(
            project_id=project.id,
            display_name=project.display_name or project.id,
            summary=clean_summary,
            sources=[clean_source],
        )

    def _project(self, project_id: str):
        from mcp_server.domain.exceptions import ManifestProjectNotFoundError

        for project in self.manifest.projects():
            if project.id == project_id:
                return project
        raise ManifestProjectNotFoundError(f'project_id "{project_id}" not declared in manifest')

    @staticmethod
    def _declared_path(root: Path, declared: str) -> Path:
        candidate = (root / declared).resolve()
        root_resolved = root.resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError("declared file path escapes project root") from exc
        return candidate


__all__ = [
    "ExplainArchitectureRequest",
    "ExplainArchitectureResult",
    "ExplainArchitectureUseCase",
]
