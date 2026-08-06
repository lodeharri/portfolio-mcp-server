"""Application use case for summarizing a manifest-declared README.

The use case reads the declared README for a project and asks the LLM
for a one-paragraph recruiter-friendly summary, max ``max_tokens``
(200 by default — see Decision #12 / change 003-playground-ui for the
short-first discipline).

Hexagonal contract: ports only — no FastAPI, no concrete adapters.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcp_server.application.ports.llm import LLMPort
from mcp_server.application.ports.manifest import ManifestPort
from mcp_server.security.audit import AuditLogger
from mcp_server.security.output_sanitizer import OutputSanitizer

MAX_README_BYTES = 32 * 1024


@dataclass(frozen=True)
class SummarizeReadmeRequest:
    project_id: str
    # Short-first principle (Decision #12): default = 200 tokens, the
    # minimum that completes a typical one-paragraph README summary.
    # Caller can override.
    max_tokens: int = 200


@dataclass(frozen=True)
class SummarizeReadmeResult:
    project_id: str
    display_name: str
    summary: str
    source: str


class SummarizeReadmeUseCase:
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

    def execute(self, request: SummarizeReadmeRequest) -> SummarizeReadmeResult:
        project = self._project(request.project_id)
        if not project.readme_path:
            raise FileNotFoundError(f"README path is not declared for project {project.id}")
        readme_file = self._declared_path(project.path, project.readme_path)
        size = readme_file.stat().st_size
        text = readme_file.read_text()
        if size > MAX_README_BYTES:
            text = text[:MAX_README_BYTES]
            self.audit.warn("llm.truncated", source="summarize_readme", bytes=size)
        summary = self.llm.summarize(text, max_tokens=request.max_tokens)
        clean_summary = self.sanitizer.sanitize(
            summary, source="summarize_readme"
        ).redacted_text
        clean_source = self.sanitizer.sanitize(
            str(readme_file), source="summarize_readme"
        ).redacted_text
        return SummarizeReadmeResult(
            project_id=project.id,
            display_name=project.display_name or project.id,
            summary=clean_summary,
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
    "SummarizeReadmeRequest",
    "SummarizeReadmeResult",
    "SummarizeReadmeUseCase",
]
