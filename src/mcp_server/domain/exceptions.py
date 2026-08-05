"""Domain exceptions — pure Python errors raised by adapters and use cases.

These exceptions are part of the domain layer (no framework deps). They
are caught at the boundary (composition root, HTTP middleware, CLI
exit codes) and translated into user-facing messages.

Hierarchy
---------

::

    McpServerError (project-wide base; adapter-friendly)
    ├── DomainError
    │   ├── ManifestSchemaError
    │   ├── ManifestNotFoundError
    │   ├── ManifestPermissionError
    │   ├── ManifestProjectNotFoundError
    │   ├── GitleaksBinaryMissingError
    │   ├── PreindexError
    │   ├── GeminiError
    │   │   ├── GeminiTransientError
    │   │   └── GeminiPermanentError
    │   └── VectorStoreError
    │       ├── EmbeddingDimensionMismatchError
    │       └── SchemaError

Two ``NewType``-style aliases:

* :class:`EmbeddingDimensionMismatchError` is also thrown directly by
  :class:`Vector`'s model validator — the use case catches it and
  re-raises as ``PreindexError(DB_ERROR)`` at the CLI boundary.
* :class:`SchemaError` is also thrown by
  :class:`mcp_server.infrastructure.db.connection` when the ``schema.sql``
  file is missing or malformed — the CLI translates it to
  ``DB_ERROR`` (exit code 5).

Naming follows ``<Layer><Reason>Error``:

* ``ManifestSchemaError`` — manifest YAML fails validation.
* ``GeminiTransientError`` — retryable API failure (5xx, 429, timeout).
* ``GeminiPermanentError`` — non-retryable API failure (4xx ≠ 429).

The CLI exit-code enum :class:`PreindexExitCode` is the SINGLE source
of truth for ``preindex`` CLI exit values. The Dockerfile's ``RUN``
line branches on these.
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "DomainError",
    "EmbeddingDimensionMismatchError",
    "GeminiError",
    "GeminiPermanentError",
    "GeminiTransientError",
    "GitleaksBinaryMissingError",
    "ManifestError",
    "ManifestNotFoundError",
    "ManifestPermissionError",
    "ManifestProjectNotFoundError",
    "ManifestSchemaError",
    "McpServerError",
    "PreindexError",
    "PreindexExitCode",
    "SchemaError",
    "VectorStoreError",
]


# ---------------------------------------------------------------------------
# Project-wide base — adapter-friendly
# ---------------------------------------------------------------------------


class McpServerError(Exception):
    """Root of the project's exception hierarchy.

    Adapters (gRPC, HTTP middleware, CLI) catch ``McpServerError`` so
    any subclass — domain, preindex, gemini, vector_store — is
    handled at the boundary without enumerating each subclass.

    Use cases SHOULD catch ``DomainError`` (the narrower pure-domain
    base) and let infrastructure exceptions like
    ``GeminiTransientError`` propagate up to the CLI/HTTP layer for
    exit-code / HTTP-status mapping.
    """


# ---------------------------------------------------------------------------
# Domain errors — pure (no framework deps)
# ---------------------------------------------------------------------------


class DomainError(McpServerError):
    """Base class for all pure domain-layer errors.

    Use cases catch ``DomainError`` to surface a unified error path at
    the composition root. Adapters raise subclasses; the CLI / HTTP
    layer maps each subclass to an exit code or HTTP status.
    """


class ManifestError(DomainError):
    """Base class for any manifest-related failure.

    The CLI catches ``ManifestError`` to map the catchable
    ``MANIFEST_ERROR`` exit code (2). Concrete subclasses
    (:class:`ManifestSchemaError`, :class:`ManifestNotFoundError`,
    :class:`ManifestPermissionError`) give finer-grained audit info.
    """


class ManifestSchemaError(ManifestError):
    """Raised when ``projects.manifest.yaml`` fails schema validation.

    Maps to preindex CLI exit code ``MANIFEST_ERROR`` (2).
    """


class ManifestNotFoundError(ManifestError):
    """Raised when the manifest path does not exist on disk.

    Maps to preindex CLI exit code ``MANIFEST_ERROR`` (2).
    """


class ManifestPermissionError(ManifestError):
    """Raised when the manifest file exists but is unreadable.

    Maps to preindex CLI exit code ``MANIFEST_ERROR`` (2).
    """


class ManifestProjectNotFoundError(ManifestError):
    """Raised when an explicit ``--project-id`` lookup misses the manifest.

    Distinct from :class:`ManifestNotFoundError` (file missing) and
    :class:`ManifestSchemaError` (schema invalid). Maps to
    ``MANIFEST_ERROR`` (2).
    """


class GitleaksBinaryMissingError(DomainError):
    """Raised when the ``gitleaks`` binary cannot be located on $PATH.

    Maps to preindex CLI exit code ``GITLEAKS_ERROR`` (3). The scanner
    MUST fail-closed on this error — the preindex pipeline aborts rather
    than indexing un-scanned chunks.
    """


class PreindexError(DomainError):
    """Raised by the preindex use case for any non-domain failure.

    Wraps infrastructure-layer errors (Gemini / DB / vector store) so
    the CLI catches a single type at the boundary. The optional
    ``exit_code`` attribute maps to :class:`PreindexExitCode`.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: PreindexExitCode | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code


# ---------------------------------------------------------------------------
# Gemini adapter exceptions — retry policy per ADR-003
# ---------------------------------------------------------------------------


class GeminiError(DomainError):
    """Base class for any Gemini SDK failure.

    The retry-aware subclasses (:class:`GeminiTransientError`,
    :class:`GeminiPermanentError`) implement ADR-003's
    fail-fast / backoff policy:

    * 429 / 5xx / connection / timeout → :class:`GeminiTransientError`
    * 4xx (≠ 429) → :class:`GeminiPermanentError`
    """


class GeminiTransientError(GeminiError):
    """Retryable Gemini API failure — 429 / 5xx / connection / timeout.

    Per ADR-003 the embedding adapter retries up to 3 attempts with
    full-jitter exponential backoff (base 1s, max 30s). After 3
    attempts the use case re-raises this as :class:`PreindexError`
    with ``exit_code=GEMINI_ERROR`` so the CLI exits with code 4.
    """


class GeminiPermanentError(GeminiError):
    """Non-retryable Gemini API failure — 4xx ≠ 429.

    Per ADR-003 the adapter fails fast (no retry). The use case
    surfaces the underlying cause (bad API key, model not found,
    payload rejected) in the audit log and exits the CLI with
    code 4 (``GEMINI_ERROR``).
    """


# ---------------------------------------------------------------------------
# Vector store exceptions
# ---------------------------------------------------------------------------


class VectorStoreError(DomainError):
    """Base class for any ``VectorStorePort`` failure.

    Maps to preindex CLI exit code ``DB_ERROR`` (5). Concrete
    subclasses carry dim/schema specifics.
    """


class EmbeddingDimensionMismatchError(VectorStoreError):
    """Raised when a vector's length does not match its declared ``embedding_dim``.

    The ``Vector`` constructor enforces this; the sqlite-vec adapter
    raises the same error when a query vector's length does not
    match the bound vec table's declared ``float[N]``.
    """


class SchemaError(VectorStoreError):
    """Raised when ``schema.sql`` is missing, unreadable, or invalid.

    The sqlite-vec adapter fails fast on this so the CLI exits with
    code 5 (``DB_ERROR``) before any chunk is embedded.
    """


# ---------------------------------------------------------------------------
# Preindex CLI exit codes
# ---------------------------------------------------------------------------


class PreindexExitCode(Enum):
    """Machine-readable exit codes for the ``preindex`` CLI.

    Per the orchestrator's PR3 spec the Dockerfile ``RUN`` line branches
    on these values. ``sys.exit(PreindexExitCode.OK.value)`` is the
    idiomatic way to exit from ``main()``.
    """

    OK = 0
    MANIFEST_ERROR = 2
    GITLEAKS_ERROR = 3
    GEMINI_ERROR = 4
    DB_ERROR = 5
