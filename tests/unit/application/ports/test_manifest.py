"""Conformance tests for ``src/mcp_server/application/ports/manifest.py``.

The :class:`ManifestPort` Protocol declares the contract a manifest adapter
must satisfy, along with the :class:`Manifest`, :class:`Project`,
:class:`ManifestServer`, :class:`IndexingConfig` Pydantic models that
define the manifest schema.

The orchestrator's PR2 spec simplifies the schema to:

* ``Manifest`` — ``server_name``, ``version``, ``default_policy``,
  ``chunk_size``, ``chunk_overlap``, ``include_extensions``, ``exclude_paths``,
  ``projects``.
* ``Project`` — ``id``, ``path``, ``display_name``, ``description``,
  ``include_subdirs``, ``exclude_subdirs``.

But the actual ``config/projects.manifest.yaml`` uses a nested
``server.{name,version,description}`` + ``indexing.{...}`` form. The
adapter uses Pydantic to flatten that file into the port's expected shape.
"""

from __future__ import annotations

import inspect
from pathlib import Path


class TestManifestModels:
    """Pydantic models for the manifest schema exist and accept valid input."""

    def test_manifest_class_exists(self) -> None:
        from mcp_server.application.ports.manifest import Manifest

        assert Manifest is not None

    def test_project_class_exists(self) -> None:
        from mcp_server.application.ports.manifest import Project

        assert Project is not None

    def test_manifest_round_trip(self) -> None:
        from mcp_server.application.ports.manifest import Manifest, Project

        m = Manifest(
            server_name="portfolio-mcp-server",
            version="0.1.0",
            default_policy="deny",
            chunk_size=1500,
            chunk_overlap=200,
            include_extensions=[".py"],
            exclude_paths=["node_modules"],
            projects=[
                Project(
                    id="finance-coach-latam",
                    path=Path("/tmp/finance"),  # noqa: S108
                    display_name="Finance",
                    description="test",
                    include_subdirs=["backend"],
                    exclude_subdirs=["node_modules"],
                )
            ],
        )
        dumped = m.model_dump()
        restored = Manifest(**dumped)
        assert restored.server_name == "portfolio-mcp-server"
        assert restored.projects[0].id == "finance-coach-latam"
        assert restored.projects[0].path == Path("/tmp/finance")  # noqa: S108


class TestManifestPortProtocol:
    """``ManifestPort`` declares the contract for manifest-backed path scoping."""

    def test_manifest_port_protocol_exists(self) -> None:
        from mcp_server.application.ports.manifest import ManifestPort

        assert ManifestPort is not None

    def test_manifest_port_has_load(self) -> None:
        from mcp_server.application.ports.manifest import ManifestPort

        members = dict(inspect.getmembers(ManifestPort))
        assert "load" in members

    def test_manifest_port_has_is_path_indexed(self) -> None:
        from mcp_server.application.ports.manifest import ManifestPort

        members = dict(inspect.getmembers(ManifestPort))
        assert "is_path_indexed" in members


class TestManifestPortConformance:
    """A class with the right methods satisfies ``ManifestPort``."""

    def test_fake_manifest_satisfies_protocol(self) -> None:
        from pathlib import Path

        from mcp_server.application.ports.manifest import (
            Manifest,
            ManifestPort,
            Project,
        )

        class FakeManifest:
            """In-memory fake satisfying ManifestPort."""

            def __init__(self) -> None:
                self._m = Manifest(
                    server_name="fake",
                    version="0.0.0",
                    default_policy="deny",
                    chunk_size=1500,
                    chunk_overlap=200,
                    include_extensions=[".py"],
                    exclude_paths=[],
                    projects=[
                        Project(
                            id="p",
                            path=Path("/tmp/p"),  # noqa: S108
                            display_name="P",
                            description="",
                            include_subdirs=["src"],
                            exclude_subdirs=[],
                        )
                    ],
                )

            def load(self) -> Manifest:
                return self._m

            def is_path_indexed(self, path: Path) -> bool:
                return str(path).startswith(str(self._m.projects[0].path))

        assert isinstance(FakeManifest(), ManifestPort)

    def test_fake_manifest_is_path_indexed_behavior(self) -> None:
        from pathlib import Path

        from mcp_server.application.ports.manifest import Manifest

        class FakeManifest:
            def __init__(self) -> None:
                self._m = Manifest(
                    server_name="x",
                    version="0.0.0",
                    default_policy="deny",
                    chunk_size=1500,
                    chunk_overlap=200,
                    include_extensions=[],
                    exclude_paths=[],
                    projects=[],
                )

            def load(self) -> Manifest:
                return self._m

            def is_path_indexed(self, path: Path) -> bool:
                return False  # default-deny

        fake = FakeManifest()
        # Default-deny even with empty manifest.
        assert fake.is_path_indexed(Path("/anywhere")) is False

    def test_class_without_methods_does_not_satisfy_protocol(self) -> None:
        from mcp_server.application.ports.manifest import ManifestPort

        class NotAManifest:
            pass

        assert not isinstance(NotAManifest(), ManifestPort)
