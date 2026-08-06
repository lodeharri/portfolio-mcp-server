"""Composition root — DI container wiring adapters to use cases.

This is the ONLY module in ``src/mcp_server/`` that imports both concrete
adapters (``infrastructure/adapters/`` and ``security/``) and use cases
(``application/use_cases/``). See
``openspec/changes/001-bootstrap/design/adrs/001-composition-eager-vs-lazy.md``
for the eager-wiring rationale (fail-fast, single source of truth, trivial
hexagonal invariant enforcement).

Change history:

* **001-bootstrap PR1** — composition root, AppConfig, /healthz,
  FastMCP mount. ``Composition`` exposes adapter fields only.
* **001-bootstrap PR2 (security-layers)** — wires the five security
  adapters: ``manifest``, ``secret_scanner``, ``sanitizer``,
  ``rate_limiter``, ``audit``.
* **001-bootstrap PR3 (preindex-pipeline)** — wires the remaining
  adapters (``embedding``, ``llm``, ``vector_store``) and the
  preindex use case (``preindex_use_case``).
* **002-mcp-tools PR1** — wires the two read-only MCP tool use
  cases (``list_projects_use_case``, ``search_use_case``). The
  remaining 4 MCP tool use cases and the LangChain ``Agent``
  land in PR2 / PR3.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mcp_server.application.ports.embedding import EmbeddingPort
from mcp_server.application.ports.llm import LLMPort
from mcp_server.application.ports.manifest import ManifestPort
from mcp_server.application.ports.rate_limiter import RateLimiterPort
from mcp_server.application.ports.secret_scanner import SecretScannerPort
from mcp_server.application.ports.vector_store import VectorStorePort
from mcp_server.application.use_cases.ask_portfolio import AskPortfolioUseCase
from mcp_server.application.use_cases.explain_architecture import ExplainArchitectureUseCase
from mcp_server.application.use_cases.get_architecture_diagram import (
    GetArchitectureDiagramUseCase,
)
from mcp_server.application.use_cases.index_project import IndexProjectUseCase
from mcp_server.application.use_cases.list_projects import ListProjectsUseCase
from mcp_server.application.use_cases.search_code import SearchCodeUseCase
from mcp_server.application.use_cases.summarize_readme import SummarizeReadmeUseCase
from mcp_server.config import AppConfig, load_config
from mcp_server.domain.exceptions import (
    ManifestError,
    PreindexExitCode,
)
from mcp_server.infrastructure.adapters.gemini_llm import (
    GeminiLlmAdapter,
    MockLlmAdapter,
)
from mcp_server.infrastructure.adapters.sqlite_vec_store import SqliteVecStore
from mcp_server.infrastructure.adapters.yaml_manifest import YamlManifestAdapter
from mcp_server.infrastructure.db.connection import open_db
from mcp_server.infrastructure.langchain import (
    create_langchain_adapter,
    create_langchain_agent,
    create_langchain_embedding,
)
from mcp_server.security.audit import AuditLogger
from mcp_server.security.gitleaks_scanner import GitleaksScanner
from mcp_server.security.output_sanitizer import OutputSanitizer
from mcp_server.security.rate_limiter import SlowapiRateLimiter


@dataclass(frozen=True)
class Composition:
    """The wired container — the output of ``create_composition()``.

    Frozen so it cannot be mutated mid-request (ADR-001 follow-up).
    Each field carries the wired adapter instance OR ``None`` when the
    adapter has not yet been implemented (002-mcp-tools PR2 / PR3).

    Fields:

    * ``config`` — typed :class:`AppConfig` (always populated).
    * ``manifest`` — :class:`ManifestPort` — PR2 (Layer 1).
    * ``embedding`` — :class:`EmbeddingPort` — PR3.
    * ``secret_scanner`` — :class:`SecretScannerPort` — PR2 (Layer 2).
    * ``vector_store`` — :class:`VectorStorePort` — PR3.
    * ``llm`` — :class:`LLMPort` — PR3.
    * ``rate_limiter`` — :class:`RateLimiterPort` — PR2 (Layer 5).
    * ``audit`` — :class:`AuditLogger` — PR2 (Layer 5).
    * ``sanitizer`` — :class:`OutputSanitizer` — PR2 (Layer 3).
    * ``preindex_use_case`` — :class:`IndexProjectUseCase` — PR3.
    * ``list_projects_use_case`` — :class:`ListProjectsUseCase` —
      002-mcp-tools PR1.
    * ``search_use_case`` — :class:`SearchCodeUseCase` — 002-mcp-tools
      PR1.
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
    list_projects_use_case: ListProjectsUseCase
    search_use_case: SearchCodeUseCase
    explain_architecture_use_case: ExplainArchitectureUseCase
    summarize_readme_use_case: SummarizeReadmeUseCase
    get_architecture_diagram_use_case: GetArchitectureDiagramUseCase
    ask_portfolio_use_case: AskPortfolioUseCase | None = None


def create_composition(
    config: AppConfig | None = None,
    *,
    use_mock_gemini: bool | None = None,
) -> Composition:
    """Build the eager-wired composition root (ADR-001).

    Args:
        config: optional :class:`AppConfig`. When ``None``, calls
            :func:`mcp_server.config.load_config` to read env vars once.
        use_mock_gemini: explicit override for the embedding/LLM
            adapter pair. When ``None`` (default), the function decides
            based on ``config.gemini_api_key``: present → real adapter,
            absent → mock adapter.

    Returns:
        A frozen :class:`Composition` instance with all 001-bootstrap
        PR3 adapters wired plus the preindex use case and the two
        002-mcp-tools PR1 read-only tool use cases.

    Raises:
        ManifestError: when the manifest is missing or fails schema
            validation. Propagated by ``YamlManifestAdapter.load()``
            which the composition root calls eagerly.
        mcp_server.domain.exceptions.SchemaError: when the bundled
            ``schema.sql`` is missing or unreadable.
    """
    if config is None:
        config = load_config()

    # Eager wiring: construct every adapter up front so adapter init
    # errors surface during ``create_composition()``, not at first
    # request / first CLI tick.
    audit = AuditLogger()
    manifest = YamlManifestAdapter(config.manifest_path)
    # Eagerly load + validate the manifest so the app fails fast on a
    # missing or schema-invalid file. Per the security-layers spec a
    # manifest without declared projects (or one that doesn't exist)
    # MUST abort the preindex pipeline with a non-zero exit code —
    # surfacing the exception here propagates it to ``create_app`` and
    # tests, which keeps the failure mode consistent with the
    # inspectable Composition error path.
    manifest.load()

    secret_scanner = GitleaksScanner(audit=audit)
    sanitizer = OutputSanitizer(audit=audit)
    rate_limiter = SlowapiRateLimiter(limit="30/minute", audit=audit)

    # PR3: vector store.
    db_path = _db_path_override(config) or (config.data_dir / "index.sqlite")
    conn = open_db(db_path)
    vector_store = SqliteVecStore(conn)

    # PR3: embedding + LLM adapter pair (mock or real).
    # Per 005-langchain-integration: embedding is delegated to the
    # LangChain adapter (``create_langchain_embedding``) so chunking,
    # agent, and embedding all share the same single wiring file. The
    # LLM adapter stays on the standalone ``google-genai`` SDK
    # (``GeminiLlmAdapter``) because LangChain's chat model is only
    # used inside the ReAct agent — the standalone chat API is
    # smaller and avoids an extra layer for the per-tool LLM calls.
    if use_mock_gemini is None:
        use_mock_gemini = not bool((config.gemini_api_key or "").strip())
    api_key = config.gemini_api_key or ""
    embedding = create_langchain_embedding(
        api_key="" if use_mock_gemini else api_key,
        model=config.gemini_embedding_model,
        embedding_dim=config.embedding_dim,
    )
    if use_mock_gemini:
        llm: LLMPort | None = MockLlmAdapter()
    else:
        llm = GeminiLlmAdapter(api_key=api_key, model=config.gemini_model)

    # PR3: preindex use case.
    chunking = create_langchain_adapter()
    preindex_use_case = IndexProjectUseCase(
        manifest=manifest,
        embedding=embedding,  # type: ignore[arg-type]
        vector_store=vector_store,  # type: ignore[arg-type]
        scanner=secret_scanner,
        chunking=chunking,
        audit=audit,
    )

    # 002-mcp-tools PR1: ``list_projects`` MCP tool use case. No LLM
    # call; just manifest + optional vector_store for chunk counts.
    list_projects_use_case = ListProjectsUseCase(
        manifest=manifest,
        vector_store=vector_store,
        sanitizer=sanitizer,
        audit=audit,
    )

    # 002-mcp-tools PR1: ``search_code`` MCP tool use case. Embeds
    # the query, runs vector search, sanitizes chunk content. The
    # embedding adapter is REQUIRED here (search has no useful
    # default without it).
    search_use_case = SearchCodeUseCase(
        embedding=embedding,  # type: ignore[arg-type]
        vector_store=vector_store,  # type: ignore[arg-type]
        sanitizer=sanitizer,
        audit=audit,
    )

    summarize_readme_use_case = SummarizeReadmeUseCase(
        manifest=manifest, llm=llm, sanitizer=sanitizer, audit=audit
    )
    explain_architecture_use_case = ExplainArchitectureUseCase(
        manifest=manifest, llm=llm, sanitizer=sanitizer, audit=audit
    )
    get_architecture_diagram_use_case = GetArchitectureDiagramUseCase(
        manifest=manifest, sanitizer=sanitizer, audit=audit
    )

    # Side-effect import: register the @mcp.tool wrappers with the
    # FastMCP instance (the decorator runs at module-load time) and
    # then populate the wrapper's use case container with the
    # instances we just built. This MUST happen after both the use
    # cases and the tools module are importable; doing it here keeps
    # the wiring in a single place (ADR-001 + composition root
    # discipline).
    from mcp_server.interfaces.mcp import tools as _tools

    _tools.set_use_cases(
        list_projects_uc=list_projects_use_case,
        search_uc=search_use_case,
        explain_architecture_uc=explain_architecture_use_case,
        summarize_readme_uc=summarize_readme_use_case,
        get_architecture_diagram_uc=get_architecture_diagram_use_case,
    )

    agent = create_langchain_agent(
        api_key=config.gemini_api_key or "",
        model=config.gemini_model,
    )
    agent_tools = [
        _tools.list_projects_tool,
        _tools.search_code_tool,
        _tools.explain_architecture_tool,
        _tools.summarize_readme_tool,
        _tools.get_architecture_diagram_tool,
    ]
    ask_portfolio_use_case = AskPortfolioUseCase(
        agent=agent,
        tools=agent_tools,
        sanitizer=sanitizer,
        audit=audit,
        rate_limiter=rate_limiter,
    )
    _tools.set_ask_portfolio_use_case(ask_portfolio_use_case)

    return Composition(
        config=config,
        manifest=manifest,
        embedding=embedding,
        secret_scanner=secret_scanner,
        vector_store=vector_store,
        llm=llm,
        rate_limiter=rate_limiter,
        audit=audit,
        sanitizer=sanitizer,
        preindex_use_case=preindex_use_case,
        list_projects_use_case=list_projects_use_case,
        search_use_case=search_use_case,
        explain_architecture_use_case=explain_architecture_use_case,
        summarize_readme_use_case=summarize_readme_use_case,
        get_architecture_diagram_use_case=get_architecture_diagram_use_case,
        ask_portfolio_use_case=ask_portfolio_use_case,
    )


def _db_path_override(config: AppConfig) -> Path | None:
    """Return the override DB path if the CLI passed one, else ``None``.

    The CLI communicates the override through a private ``_db_path_override``
    attribute (set via ``model_copy``); composition reads it here. This is
    the ONLY escape hatch from the typed config — kept narrow.
    """
    raw = getattr(config, "_db_path_override", None)
    if isinstance(raw, Path):
        return raw
    return None


#: Default Gemini chat model for the ask_portfolio LangChain agent.
#: Matches ``gemini_llm.DEFAULT_MODEL``. The composition root reads
#: the model from ``config.gemini_model`` (overridable via the
#: ``GEMINI_MODEL`` env var) — this constant stays as the canonical
#: default declared in :class:`AppConfig`. (ADR-001: no scattered
#: configuration.)
AGENT_MODEL_NAME: str = "gemini-flash-latest"


# Alias matching design.md and tasks.md. ``compose`` reads more naturally
# inside the composition-root pattern; ``create_composition`` is the
# explicit factory name requested by the PR1 orchestrator prompt.
compose = create_composition


__all__ = [
    "Composition",
    "PreindexExitCode",  # re-export for legacy callers
    "compose",
    "create_composition",
]

# Re-export so CLI imports ``PreindexExitCode`` from composition if it
# prefers; the canonical home is ``mcp_server.domain.exceptions``.
_ = ManifestError  # silence linter about unused import (intentional re-export)
