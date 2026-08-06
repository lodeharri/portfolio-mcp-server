"""Unit tests for SummarizeReadmeUseCase."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_server.application.ports.manifest import Manifest, Project
from mcp_server.application.use_cases.summarize_readme import (
    SummarizeReadmeRequest,
    SummarizeReadmeUseCase,
)
from mcp_server.security.output_sanitizer import OutputSanitizer


class FakeManifest:
    def __init__(self, project: Project):
        self.project = project

    def load(self) -> Manifest:
        return Manifest(server_name="test", version="1", projects=[self.project])

    def projects(self) -> list[Project]:
        return [self.project]

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


def test_reads_readme_with_default_token_budget(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("A portfolio project built with Astro.")
    project = Project(id="demo", path=tmp_path, readme_path="README.md")
    llm = FakeLlm("A concise summary")
    audit = FakeAudit()
    uc = SummarizeReadmeUseCase(
        manifest=FakeManifest(project), llm=llm,
        sanitizer=OutputSanitizer(audit=audit), audit=audit,
    )

    result = uc.execute(SummarizeReadmeRequest(project_id="demo"))

    assert result.project_id == "demo"
    assert result.source == str(readme)
    assert result.summary == "A concise summary"
    assert llm.calls == [(readme.read_text(), 200)]


def test_default_max_tokens_is_200(tmp_path: Path) -> None:
    """Decision #12 / change 003-playground-ui: the default ``max_tokens``
    for ``summarize_readme`` is **200** (was 300). A fresh request
    without an explicit ``max_tokens`` MUST call the LLM with
    ``max_tokens=200``.
    """
    readme = tmp_path / "README.md"
    readme.write_text("A recruiter-facing overview.")
    project = Project(id="demo", path=tmp_path, readme_path="README.md")
    llm = FakeLlm("summary")
    audit = FakeAudit()
    uc = SummarizeReadmeUseCase(
        manifest=FakeManifest(project), llm=llm,
        sanitizer=OutputSanitizer(audit=audit), audit=audit,
    )

    uc.execute(SummarizeReadmeRequest(project_id="demo"))

    assert llm.calls == [(readme.read_text(), 200)]


def test_explicit_max_tokens_overrides_default(tmp_path: Path) -> None:
    """The default 200 is the *floor*, not a *ceiling*. A
    caller-supplied ``max_tokens=500`` MUST be passed through to the LLM.
    """
    readme = tmp_path / "README.md"
    readme.write_text("Long-form README content.")
    project = Project(id="demo", path=tmp_path, readme_path="README.md")
    llm = FakeLlm("summary")
    audit = FakeAudit()
    uc = SummarizeReadmeUseCase(
        manifest=FakeManifest(project), llm=llm,
        sanitizer=OutputSanitizer(audit=audit), audit=audit,
    )

    uc.execute(SummarizeReadmeRequest(project_id="demo", max_tokens=500))

    assert llm.calls == [(readme.read_text(), 500)]


def test_sanitizes_summary_and_falls_back_to_id(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("Setup instructions")
    project = Project(id="demo", path=tmp_path, display_name="", readme_path="README.md")
    llm = FakeLlm("api_key=abc123secret")
    audit = FakeAudit()
    uc = SummarizeReadmeUseCase(
        manifest=FakeManifest(project), llm=llm,
        sanitizer=OutputSanitizer(audit=audit), audit=audit,
    )

    result = uc.execute(SummarizeReadmeRequest(project_id="demo"))

    assert result.display_name == "demo"
    assert result.summary == "[REDACTED]"
    assert any(event == "output.redacted" for event, _ in audit.events)


def test_truncates_large_readme(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("r" * 40000)
    project = Project(id="demo", path=tmp_path, readme_path="README.md")
    llm = FakeLlm("summary")
    audit = FakeAudit()
    uc = SummarizeReadmeUseCase(
        manifest=FakeManifest(project), llm=llm,
        sanitizer=OutputSanitizer(audit=audit), audit=audit,
    )

    uc.execute(SummarizeReadmeRequest(project_id="demo"))

    assert len(llm.calls[0][0]) == 32768
    assert any(event == "llm.truncated" for event, _ in audit.events)
