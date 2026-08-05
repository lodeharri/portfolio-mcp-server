"""Domain exceptions — pure Python errors raised by adapters and use cases.

These exceptions are part of the domain layer (no framework deps). They
are caught at the boundary (composition root, HTTP middleware, CLI
exit codes) and translated into user-facing messages.

PR2 seeds the manifest-related exceptions (``ManifestSchemaError``,
``ManifestNotFoundError``, ``ManifestPermissionError``) and the
gitleaks-related exception (``GitleaksBinaryMissingError``). PR3 adds
the remaining errors (``GeminiTransientError``, ``GeminiPermanentError``,
``PreindexAbortedError``) per the design.md contract.

The naming follows ``<Layer><Reason>Error`` — e.g. ``ManifestSchemaError``
= the manifest's schema is invalid; ``GitleaksBinaryMissingError`` = the
gitleaks binary is missing on $PATH.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all domain-layer errors.

    Use cases catch ``DomainError`` to surface a unified error path at the
    composition root. Adapters raise subclasses; the CLI / HTTP layer
    maps each subclass to an exit code or HTTP status.
    """


class ManifestSchemaError(DomainError):
    """Raised when ``projects.manifest.yaml`` fails schema validation.

    Maps to preindex CLI exit code ``MANIFEST_ERROR`` (2).
    """


class ManifestNotFoundError(DomainError):
    """Raised when the manifest path does not exist on disk.

    Maps to preindex CLI exit code ``MANIFEST_ERROR`` (2).
    """


class ManifestPermissionError(DomainError):
    """Raised when the manifest file exists but is unreadable.

    Maps to preindex CLI exit code ``MANIFEST_ERROR`` (2).
    """


class GitleaksBinaryMissingError(DomainError):
    """Raised when the ``gitleaks`` binary cannot be located on $PATH.

    Maps to preindex CLI exit code ``GITLEAKS_ERROR`` (3). The scanner
    MUST fail-closed on this error — the preindex pipeline aborts rather
    than indexing un-scanned chunks.
    """


__all__ = [
    "DomainError",
    "GitleaksBinaryMissingError",
    "ManifestNotFoundError",
    "ManifestPermissionError",
    "ManifestSchemaError",
]