"""YAML-backed manifest adapter — implements ``ManifestPort``.

Layer 1 of the 5-layer security model. Reads
``config/projects.manifest.yaml``, flattens the on-disk nested structure
(``server.{name,version,description}`` + ``indexing.{...}``) into the
flat :class:`Manifest` shape declared by the application port, and
answers ``is_path_indexed(path)`` with **default-deny** semantics:

* Only paths under a declared project's ``include_subdirs`` may be
  indexed.
* Paths in a project's ``exclude_subdirs`` are denied even if they are
  under an ``include_subdirs``.
* Paths outside any declared project return ``False``.
* Path traversal (``../``) that resolves outside the project root
  returns ``False``.

Construction is eager (ADR-001): the adapter accepts the manifest path
at ``__init__`` and parses + flattens on the first ``load()`` call. The
composition root holds a single instance for the lifetime of the app.

Threat-matrix coverage
----------------------

* "Path traversal in is_path_indexed" — handled by resolving the input
  path against the project root and checking the resolved path stays
  within bounds. ``..`` attempts resolve outside the root and are
  denied.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from mcp_server.application.ports.manifest import Manifest, ManifestPort, Project
from mcp_server.domain.exceptions import (
    ManifestNotFoundError,
    ManifestPermissionError,
    ManifestSchemaError,
)


# ---------------------------------------------------------------------------
# Internal Pydantic model — mirrors the on-disk YAML schema
# ---------------------------------------------------------------------------


class _ManifestServer(BaseModel):
    """Nested ``server:`` section in the YAML."""

    name: str
    version: str
    description: str = ""


class _IndexingConfig(BaseModel):
    """Nested ``indexing:`` section in the YAML."""

    default_policy: str = "deny"
    chunk_size: int = 1500
    chunk_overlap: int = 200
    include_extensions: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)


class _RawProject(BaseModel):
    """Per-project entry in the on-disk YAML.

    Extra fields (``adr_path``, ``readme_path``) are tolerated because
    the application-layer :class:`Project` strips them. They are not
    used by PR2 but kept on disk for documentation.
    """

    id: str
    path: Path
    display_name: str = ""
    description: str = ""
    include_subdirs: list[str] = Field(default_factory=list)
    exclude_subdirs: list[str] = Field(default_factory=list)


class _RawManifest(BaseModel):
    """Internal model mirroring the on-disk YAML.

    The :class:`YamlManifestAdapter` flattens this into the application
    layer's :class:`Manifest` shape (``server_name`` at the top level,
    etc.).
    """

    schema_version: int
    server: _ManifestServer
    indexing: _IndexingConfig
    projects: list[_RawProject] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class YamlManifestAdapter:
    """Concrete adapter implementing :class:`ManifestPort`.

    Args:
        manifest_path: Path to ``projects.manifest.yaml``. May be
            relative to the current working directory; the adapter does
            NOT touch ``os.environ``, so relative paths are resolved at
            call time by the caller (composition root / CLI).
    """

    def __init__(self, manifest_path: Path | str) -> None:
        self._path = Path(manifest_path)
        self._manifest: Manifest | None = None  # cached after first load()

    def load(self) -> Manifest:
        """Parse the YAML and return the flattened :class:`Manifest`.

        Raises:
            ManifestNotFoundError: ``manifest_path`` does not exist.
            ManifestPermissionError: file exists but is unreadable.
            ManifestSchemaError: YAML is valid but does not satisfy the
                schema (missing ``schema_version`` / ``server`` /
                ``projects``).
        """
        if self._manifest is not None:
            return self._manifest

        if not self._path.exists():
            raise ManifestNotFoundError(f"manifest not found: {self._path}")
        try:
            raw_text = self._path.read_text()
        except PermissionError as exc:
            raise ManifestPermissionError(
                f"manifest is unreadable: {self._path}"
            ) from exc

        try:
            raw_dict = yaml.safe_load(raw_text)
        except yaml.YAMLError as exc:
            raise ManifestSchemaError(
                f"manifest YAML is malformed: {exc}"
            ) from exc

        try:
            raw = _RawManifest.model_validate(raw_dict)
        except Exception as exc:
            raise ManifestSchemaError(
                f"manifest does not match schema: {exc}"
            ) from exc

        self._manifest = Manifest(
            server_name=raw.server.name,
            version=raw.server.version,
            default_policy=raw.indexing.default_policy,
            chunk_size=raw.indexing.chunk_size,
            chunk_overlap=raw.indexing.chunk_overlap,
            include_extensions=list(raw.indexing.include_extensions),
            exclude_paths=list(raw.indexing.exclude_paths),
            projects=[
                Project(
                    id=p.id,
                    path=p.path,
                    display_name=p.display_name,
                    description=p.description,
                    include_subdirs=list(p.include_subdirs),
                    exclude_subdirs=list(p.exclude_subdirs),
                )
                for p in raw.projects
            ],
        )
        return self._manifest

    def is_path_indexed(self, path: Path) -> bool:
        """Default-deny path-scoping check (Layer 1).

        Returns ``True`` iff:

        * ``path`` resolves to a location under one of the declared
          project's roots, AND
        * ``path`` is under one of that project's ``include_subdirs``,
          AND
        * ``path`` is NOT under any of the project's ``exclude_subdirs``,
          AND
        * ``path``'s extension is in the manifest's ``include_extensions``.
        """
        manifest = self.load()
        try:
            resolved = Path(path).resolve(strict=False)
        except (OSError, RuntimeError):
            return False

        for project in manifest.projects:
            project_root = Path(project.path).resolve(strict=False)
            try:
                rel = resolved.relative_to(project_root)
            except ValueError:
                continue  # not under this project

            # Must be under one of the declared include_subdirs.
            if not self._under_any(rel, project.include_subdirs):
                continue

            # Must NOT be under any of the exclude_subdirs.
            if self._under_any(rel, project.exclude_subdirs):
                continue

            # File extension must be in the manifest's whitelist.
            if manifest.include_extensions:
                ext = resolved.suffix.lower()
                if ext not in {e.lower() for e in manifest.include_extensions}:
                    continue

            return True

        return False

    @staticmethod
    def _under_any(rel: Path, prefixes: list[str]) -> bool:
        """Return True if ``rel`` starts with any of the listed subdir names.

        Matches the first path segment so ``include_subdirs: ["src"]``
        allows ``src/foo/bar.py`` but not ``foo/src/bar.py``.
        """
        if not prefixes:
            return False
        rel_parts = rel.parts
        if not rel_parts:
            return False
        return rel_parts[0] in prefixes


__all__ = ["YamlManifestAdapter"]