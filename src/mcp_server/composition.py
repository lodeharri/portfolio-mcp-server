"""Composition root — DI container wiring adapters to use cases.

This is the ONLY module in ``src/mcp_server/`` that imports both concrete
adapters (``infrastructure/adapters/`` and ``security/``) and use cases
(``application/use_cases/``). See
``openspec/changes/001-bootstrap/design/adrs/001-composition-eager-vs-lazy.md``
for the eager-wiring rationale (fail-fast, single source of truth, trivial
hexagonal invariant enforcement).

PR1 scope: this module exposes the :class:`Composition` dataclass with all
adapter fields as ``None`` placeholders.

PR2 (``security-layers``) wires the five security adapters:

* ``manifest`` — :class:`YamlManifestAdapter` (Layer 1)
* ``secret_scanner`` — :class:`GitleaksScanner` (Layer 2)
* ``sanitizer`` — :class:`OutputSanitizer` (Layer 3, also wired as
  middleware in ``interfaces/http/middleware/sanitizer.py``)
* ``rate_limiter`` — :class:`SlowapiRateLimiter` (Layer 5)
* ``audit`` — :class:`AuditLogger` (Layer 5)

The remaining placeholders (``embedding``, ``vector_store``, ``llm``,
``preindex_use_case``, ``search_use_case``, ``list_projects_use_case``)
stay ``None`` until PR3 / 002-mcp-tools land.
"""

from __future__ import annotations

from dataclasses import dataclass

from mcp_server.application.ports.embedding import EmbeddingPort
from mcp_server.application.ports.llm import LLMPort
from mcp_server.application.ports.manifest import ManifestPort
from mcp_server.application.ports.rate_limiter import RateLimiterPort
from mcp_server.application.ports.secret_scanner import SecretScannerPort
from mcp_server.application.ports.vector_store import VectorStorePort
from mcp_server.config import AppConfig, load_config
from mcp_server.infrastructure.adapters.yaml_manifest import YamlManifestAdapter
from mcp_server.security.audit import AuditLogger
from mcp_server.security.gitleaks_scanner import GitleaksScanner
from mcp_server.security.output_sanitizer import OutputSanitizer
from mcp_server.security.rate_limiter import SlowapiRateLimiter


@dataclass(frozen=True)
class Composition:
    """The wired container — the output of ``create_composition()``.

    Frozen so it cannot be mutated mid-request (ADR-001 follow-up).
    Each field carries the wired adapter instance OR ``None`` when the
    adapter has not yet been implemented (PR3 / 002-mcp-tools).

    Fields:

    * ``config`` — typed :class:`AppConfig` (always populated)
    * ``manifest`` — :class:`ManifestPort` — PR2 (Layer 1)
    * ``embedding`` — :class:`EmbeddingPort` — PR3
    * ``secret_scanner`` — :class:`SecretScannerPort` — PR2 (Layer 2)
    * ``vector_store`` — :class:`VectorStorePort` — PR3
    * ``rate_limiter`` — :class:`RateLimiterPort` — PR2 (Layer 5)
    * ``audit`` — :class:`AuditLogger` — PR2 (Layer 5)
    * ``sanitizer`` — :class:`OutputSanitizer` — PR2 (Layer 3)
    * ``preindex_use_case`` — PR3
    * ``search_use_case`` — 002-mcp-tools
    * ``list_projects_use_case`` — 002-mcp-tools
    """

    config: AppConfig
    manifest: ManifestPort
    embedding: EmbeddingPort | None
    secret_scanner: SecretScannerPort
    vector_store: VectorStorePort | None
    llm: LLMPort | None
    rate_limiter: RateLimiterPort
    audit: AuditLogger
    sanitizer: OutputSanitizer
    preindex_use_case: object | None
    search_use_case: object | None
    list_projects_use_case: object | None


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

    # Eager wiring: construct every adapter up front so adapter init
    # errors surface during create_app(), not at first request.
    audit = AuditLogger()
    manifest = YamlManifestAdapter(config.manifest_path)
    secret_scanner = GitleaksScanner(audit=audit)
    sanitizer = OutputSanitizer()
    rate_limiter = SlowapiRateLimiter(limit="30/minute", audit=audit)

    return Composition(
        config=config,
        manifest=manifest,
        embedding=None,  # wired in PR3 (GeminiEmbeddingAdapter)
        secret_scanner=secret_scanner,
        vector_store=None,  # wired in PR3 (SqliteVecStore)
        llm=None,  # wired in 002-mcp-tools (GeminiLLMAdapter)
        rate_limiter=rate_limiter,
        audit=audit,
        sanitizer=sanitizer,
        preindex_use_case=None,  # wired in PR3
        search_use_case=None,  # wired in 002-mcp-tools
        list_projects_use_case=None,  # wired in 002-mcp-tools
    )


# Alias matching design.md and tasks.md. ``compose`` reads more naturally
# inside the composition-root pattern; ``create_composition`` is the
# explicit factory name requested by the PR1 orchestrator prompt.
compose = create_composition


__all__ = ["Composition", "compose", "create_composition"]
