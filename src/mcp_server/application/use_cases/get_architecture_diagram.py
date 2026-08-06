"""Application use case for returning a sanitized base64 SVG diagram."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from mcp_server.application.ports.manifest import ManifestPort
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer

MAX_DIAGRAM_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class GetArchitectureDiagramRequest:
    project_id: str


@dataclass(frozen=True)
class GetArchitectureDiagramResult:
    project_id: str
    display_name: str
    media_type: str
    encoding: str
    data: str
    source: str


class GetArchitectureDiagramUseCase:
    def __init__(
        self,
        *,
        manifest: ManifestPort,
        sanitizer: OutputSanitizer,
        audit: AuditLogger,
    ) -> None:
        self.manifest = manifest
        self.sanitizer = sanitizer
        self.audit = audit

    def execute(self, request: GetArchitectureDiagramRequest) -> GetArchitectureDiagramResult:
        project = self._project(request.project_id)
        if not project.diagram_path:
            raise FileNotFoundError(f"diagram path is not declared for project {project.id}")
        diagram_file = self._declared_path(project.path, project.diagram_path)
        size = diagram_file.stat().st_size
        if size > MAX_DIAGRAM_BYTES:
            raise ValueError("diagram exceeds 10 MB size cap")
        raw = diagram_file.read_bytes()
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("diagram content is not SVG") from exc
        if not decoded.lstrip().startswith(("<svg", "<?xml")):
            raise ValueError("diagram content is not SVG")
        clean_svg = self.sanitizer.sanitize(
            decoded, source="get_architecture_diagram"
        ).redacted_text
        clean_source = self.sanitizer.sanitize(
            str(diagram_file), source="get_architecture_diagram"
        ).redacted_text
        self.audit.info("tool.completed", source="get_architecture_diagram", bytes=size)
        return GetArchitectureDiagramResult(
            project_id=project.id,
            display_name=project.display_name or project.id,
            media_type="image/svg+xml",
            encoding="base64",
            data=base64.b64encode(clean_svg.encode("utf-8")).decode("ascii"),
            source=clean_source,
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
        try:
            candidate.relative_to(root.resolve())
        except ValueError as exc:
            raise ValueError("declared file path escapes project root") from exc
        return candidate


__all__ = [
    "GetArchitectureDiagramRequest",
    "GetArchitectureDiagramResult",
    "GetArchitectureDiagramUseCase",
]
