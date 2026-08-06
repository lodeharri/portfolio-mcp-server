"""Unit tests for ExplainArchitectureUseCase."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcp_server.application.ports.manifest import Manifest, Project
from mcp_server.application.use_cases.explain_architecture import (
    ExplainArchitectureRequest,
    ExplainArchitectureUseCase,
)
from mcp_server.domain.exceptions import ManifestProjectNotFoundError
from mcp_server.security.output_sanitizer import OutputSanitizer


class FakeManifest:
    def __init__(self, projects: list[Project]):
        self._projects = projects

    def load(self) -> Manifest:
        return Manifest(server_name="test", version="1", projects=self._projects)

    def projects(self) -> list[Project]:
        return list(self._projects)

    def is_path_indexed(self, path: Path) -> bool:
        return False


class FakeLlm:
    def __init__(self, summary: str):
        self.summary = summary
        self.calls: list[tuple[str, int]] = []

    def summarize(self, text: str, max_tokens: int = 500) -> str:
        self.calls.append((text, max_tokens))
        return self.summary

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> str:
        return ""


class FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warn(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def make_project(
    root: Path, *, adr_path: str = "docs/design.md", display_name: str = "Demo"
) -> Project:
    return Project(id="demo", path=root, display_name=display_name, adr_path=adr_path)


def make_use_case(root: Path, summary: str = "clean summary"):
    llm = FakeLlm(summary)
    audit = FakeAudit()
    uc = ExplainArchitectureUseCase(
        manifest=FakeManifest([make_project(root)]),
        llm=llm,
        sanitizer=OutputSanitizer(audit=audit),
        audit=audit,
    )
    return uc, llm, audit


def test_reads_declared_adr_and_summarizes_once(tmp_path: Path) -> None:
    adr = tmp_path / "docs" / "design.md"
    adr.parent.mkdir()
    adr.write_text("Architecture uses ports and adapters.")
    uc, llm, _ = make_use_case(tmp_path)

    result = uc.execute(ExplainArchitectureRequest(project_id="demo", max_tokens=42))

    assert result.project_id == "demo"
    assert result.sources == [str(adr)]
    assert result.summary == "clean summary"
    assert llm.calls == [(adr.read_text(), 42)]


def test_sanitizes_model_output_and_falls_back_to_id(tmp_path: Path) -> None:
    adr = tmp_path / "docs" / "design.md"
    adr.parent.mkdir()
    adr.write_text("Architecture.")
    project = make_project(tmp_path, display_name="")
    audit = FakeAudit()
    llm = FakeLlm("token=secret-value")
    uc = ExplainArchitectureUseCase(
        manifest=FakeManifest([project]), llm=llm,
        sanitizer=OutputSanitizer(audit=audit), audit=audit,
    )

    result = uc.execute(ExplainArchitectureRequest(project_id="demo"))

    assert result.display_name == "demo"
    assert result.summary == "[REDACTED]"
    assert any(event == "output.redacted" for event, _ in audit.events)


def test_missing_project_raises_domain_error(tmp_path: Path) -> None:
    uc, _, _ = make_use_case(tmp_path)

    with pytest.raises(ManifestProjectNotFoundError):
        uc.execute(ExplainArchitectureRequest(project_id="missing"))


def test_missing_adr_raises_file_not_found(tmp_path: Path) -> None:
    uc, _, _ = make_use_case(tmp_path)

    with pytest.raises(FileNotFoundError):
        uc.execute(ExplainArchitectureRequest(project_id="demo"))


def test_truncates_large_adr(tmp_path: Path) -> None:
    adr = tmp_path / "docs" / "design.md"
    adr.parent.mkdir()
    adr.write_text("x" * 70000)
    uc, llm, audit = make_use_case(tmp_path)

    uc.execute(ExplainArchitectureRequest(project_id="demo"))

    assert len(llm.calls[0][0]) == 65536
    assert any(event == "llm.truncated" for event, _ in audit.events)
