"""Unit tests for GetArchitectureDiagramUseCase."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from mcp_server.application.ports.manifest import Manifest, Project
from mcp_server.application.use_cases.get_architecture_diagram import (
    GetArchitectureDiagramRequest,
    GetArchitectureDiagramUseCase,
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


class FakeAudit:
    def __init__(self):
        self.events: list[tuple[str, dict[str, Any]]] = []

    def warn(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self.events.append((event, fields))


def make_use_case(root: Path):
    project = Project(
        id="demo", path=root, display_name="Demo", diagram_path="docs/architecture.svg"
    )
    audit = FakeAudit()
    return GetArchitectureDiagramUseCase(
        manifest=FakeManifest(project), sanitizer=OutputSanitizer(audit=audit), audit=audit
    ), audit


def test_returns_lossless_base64_svg(tmp_path: Path) -> None:
    source = b'<svg xmlns="http://www.w3.org/2000/svg"><rect /></svg>'
    path = tmp_path / "docs" / "architecture.svg"
    path.parent.mkdir()
    path.write_bytes(source)
    uc, audit = make_use_case(tmp_path)

    result = uc.execute(GetArchitectureDiagramRequest(project_id="demo"))

    assert result.media_type == "image/svg+xml"
    assert result.encoding == "base64"
    assert base64.b64decode(result.data) == source
    assert result.source == str(path)
    assert any(event == "tool.completed" for event, _ in audit.events)


def test_sanitizes_svg_before_encoding(tmp_path: Path) -> None:
    source = b'<svg><text>password=hunter2</text></svg>'
    path = tmp_path / "docs" / "architecture.svg"
    path.parent.mkdir()
    path.write_bytes(source)
    uc, audit = make_use_case(tmp_path)

    result = uc.execute(GetArchitectureDiagramRequest(project_id="demo"))

    decoded = base64.b64decode(result.data).decode()
    assert decoded == "<svg><text>[REDACTED]</text></svg>"
    assert any(event == "output.redacted" for event, _ in audit.events)


def test_rejects_non_svg(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "architecture.svg"
    path.parent.mkdir()
    path.write_bytes(b"\x89PNG\r\n")
    uc, _ = make_use_case(tmp_path)

    with pytest.raises(ValueError, match="SVG"):
        uc.execute(GetArchitectureDiagramRequest(project_id="demo"))


def test_rejects_files_over_10_mb(tmp_path: Path) -> None:
    path = tmp_path / "docs" / "architecture.svg"
    path.parent.mkdir()
    with path.open("wb") as handle:
        handle.write(b"<svg>")
        handle.truncate(10 * 1024 * 1024 + 1)
    uc, _ = make_use_case(tmp_path)

    with pytest.raises(ValueError, match="10 MB"):
        uc.execute(GetArchitectureDiagramRequest(project_id="demo"))
