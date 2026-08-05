"""Manifest port — application-layer contract for path scoping (Layer 1).

Layer 1 of the 5-layer security model. ``ManifestPort.is_path_indexed``
is the ONLY function that decides which filesystem paths are eligible
for indexing. The default is deny: any path not explicitly listed in a
declared project's ``include_subdirs`` returns ``False``.

Schema follows the orchestrator's PR2 spec:

* :class:`Manifest` — flat shape with ``server_name``, ``version``,
  ``default_policy``, ``chunk_size``, ``chunk_overlap``,
  ``include_extensions``, ``exclude_paths``, ``projects``.
* :class:`Project` — per-project declaration: ``id``, ``path``,
  ``display_name``, ``description``, ``include_subdirs``,
  ``exclude_subdirs``.

The concrete YAML adapter (``infrastructure/adapters/yaml_manifest.py``)
reads ``config/projects.manifest.yaml`` (which uses a nested
``server.{name,version}`` / ``indexing.{...}`` form) and flattens it to
the :class:`Manifest` shape defined here.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from typing import Protocol, runtime_checkable


class Project(BaseModel):
    """A single project declared in the manifest.

    The ``path`` is absolute and points at the project root. ``include_subdirs``
    and ``exclude_subdirs`` are relative path segments under that root.
    """

    id: str
    path: Path
    display_name: str
    description: str
    include_subdirs: list[str] = Field(default_factory=list)
    exclude_subdirs: list[str] = Field(default_factory=list)


class Manifest(BaseModel):
    """The full manifest, flattened to the application-layer contract.

    Note: the on-disk YAML uses ``server.{name,version,description}`` and
    ``indexing.{default_policy,chunk_size,chunk_overlap,...}``. The
    ``YamlManifestAdapter`` flattens those nested sections into the
    top-level fields below so the application layer never sees YAML
    structure.
    """

    server_name: str
    version: str
    default_policy: str = "deny"
    chunk_size: int = 1500
    chunk_overlap: int = 200
    include_extensions: list[str] = Field(default_factory=list)
    exclude_paths: list[str] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)


@runtime_checkable
class ManifestPort(Protocol):
    """Contract for any manifest-backed path-scoping adapter.

    The protocol is deliberately tiny: ``load()`` returns the parsed
    manifest, ``is_path_indexed(path)`` answers the security-critical
    question for the preindex pipeline.
    """

    def load(self) -> Manifest:
        """Return the parsed :class:`Manifest`.

        Implementations MAY cache the parsed manifest after the first
        call. The composition root calls ``load()`` once at startup
        (ADR-001 eager wiring).
        """
        ...

    def is_path_indexed(self, path: Path) -> bool:
        """Return ``True`` iff ``path`` is eligible for indexing.

        Default-deny: any path that is not under a declared project's
        ``include_subdirs`` (and not in any ``exclude_subdirs``) returns
        ``False``. Path traversal (``../``) and absolute paths outside
        declared roots also return ``False``.
        """
        ...


__all__ = ["Manifest", "ManifestPort", "Project"]