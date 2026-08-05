"""Composition root — DI container wiring adapters to use cases.

This is the ONLY module in ``src/mcp_server/`` that imports both concrete
adapters (``infrastructure/adapters/``) and use cases
(``application/use_cases/``). See
``openspec/changes/001-bootstrap/design/adrs/001-composition-eager-vs-lazy.md``
for the eager-wiring rationale (fail-fast, single source of truth, trivial
hexagonal invariant enforcement).

PR1 scope: this module exposes the :class:`Composition` dataclass with all
adapter fields as ``None`` placeholders. Subsequent change-PRs replace them
with real adapters:

* PR2 (``security-layers``) — ``manifest_port``, ``scanner_port``,
  ``sanitizer``, ``rate_limiter``, ``audit``
* PR3 (``preindex-pipeline``) — ``embedding_port``, ``vector_port``,
  ``preindex_use_case``
* ``002-mcp-tools`` — ``search_use_case``, ``list_projects_use_case``
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from mcp_server.config import AppConfig, load_config


@dataclass(frozen=True)
class Composition:
    """The wired container — the output of ``create_composition()``.

    Frozen so it cannot be mutated mid-request (ADR-001 follow-up). Each
    field carries the wired adapter instance OR ``None`` when the adapter
    has not yet been implemented (PR1 baseline).

    Fields (PR1 = all ``None`` for adapters not yet wired):

    * ``config`` — typed :class:`AppConfig` (always populated)
    * ``manifest_port`` — :class:`ManifestPort` — PR2
    * ``embedding_port`` — :class:`EmbeddingPort` — PR3
    * ``scanner_port`` — :class:`SecretScannerPort` — PR2
    * ``vector_port`` — :class:`VectorStorePort` — PR3
    * ``rate_limiter`` — :class:`RateLimiterPort` — PR2
    * ``audit`` — :class:`AuditLogger` — PR2
    * ``sanitizer`` — :class:`OutputSanitizer` — PR2
    * ``preindex_use_case`` — :class:`PreindexUseCase` — PR3
    * ``search_use_case`` — :class:`SearchCodeUseCase` — 002-mcp-tools
    * ``list_projects_use_case`` — :class:`ListProjectsUseCase` — 002-mcp-tools
    """

    config: AppConfig
    # The adapter fields are typed ``Any`` for now because importing the
    # Protocol types would create forward references to modules that don't
    # exist yet. Once PR2 introduces ``application/ports/*.py`` Protocols
    # we tighten the annotations. Behavior is unaffected: each field is
    # either an instance of its Protocol OR ``None``.
    manifest_port: Any
    embedding_port: Any
    scanner_port: Any
    vector_port: Any
    rate_limiter: Any
    audit: Any
    sanitizer: Any
    preindex_use_case: Any
    search_use_case: Any
    list_projects_use_case: Any


def create_composition(config: AppConfig | None = None) -> Composition:
    """Build the eager-wired composition root (ADR-001).

    Args:
        config: optional :class:`AppConfig`. When ``None``, calls
            :func:`mcp_server.config.load_config` to read env vars once.

    Returns:
        A frozen :class:`Composition` instance. Calling this function twice
        returns two distinct instances — there is no module-level cache
        (testability + isolation guarantee from ADR-001).

    Raises:
        pydantic.ValidationError: when env vars are invalid (propagated
            from :func:`load_config`).
    """
    if config is None:
        config = load_config()
    return Composition(
        config=config,
        manifest_port=None,
        embedding_port=None,
        scanner_port=None,
        vector_port=None,
        rate_limiter=None,
        audit=None,
        sanitizer=None,
        preindex_use_case=None,
        search_use_case=None,
        list_projects_use_case=None,
    )


# Alias matching design.md and tasks.md. ``compose`` reads more naturally
# inside the composition-root pattern; ``create_composition`` is the
# explicit factory name requested by the PR1 orchestrator prompt.
compose: Final = create_composition


__all__ = ["Composition", "compose", "create_composition"]
