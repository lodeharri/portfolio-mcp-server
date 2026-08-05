"""Unit tests for ``src/mcp_server/domain/exceptions.py``.

The domain exception hierarchy is the single source of error semantics
across the application. The hierarchy (after PR3 expansion):

* ``McpServerError`` — project-wide base (adapter-friendly)
* ``DomainError`` — pure domain errors
  * ``ManifestSchemaError`` — PR2 seed
  * ``ManifestNotFoundError`` — PR2 seed
  * ``ManifestPermissionError`` — PR2 seed
  * ``GitleaksBinaryMissingError`` — PR2 seed
  * ``ManifestProjectNotFoundError`` — PR3 (referenced-but-missing project)
  * ``GeminiError`` — PR3 base for embedding/LLM adapters
    * ``GeminiTransientError`` — retryable (5xx, 429, timeout)
    * ``GeminiPermanentError`` — non-retryable (4xx ≠ 429)
  * ``VectorStoreError`` — PR3 base for vector store I/O failures
    * ``EmbeddingDimensionMismatchError`` — PR3 dim mismatch
    * ``SchemaError`` — PR3 schema.sql validation
  * ``PreindexError`` — PR3 base for the preindex use case
  * ``PreindexExitCode`` — PR3 exit-code enum (OK=0 … DB_ERROR=5)

Tests assert:

1. The hierarchy is correct (subclass relationships hold).
2. ``PreindexExitCode`` has the documented values.
3. Catching the broad base is sufficient for use-case / CLI error handling.
"""

from __future__ import annotations

import enum

import pytest


class TestMcPServerErrorBase:
    """``McpServerError`` is the project-wide root for adapter errors."""

    def test_mcp_server_error_can_be_imported(self) -> None:
        from mcp_server.domain.exceptions import McpServerError

        assert McpServerError is not None

    def test_domain_error_inherits_from_mcp_server_error(self) -> None:
        from mcp_server.domain.exceptions import DomainError, McpServerError

        assert issubclass(DomainError, McpServerError)

    def test_manifest_schema_error_inherits_from_domain_error(self) -> None:
        from mcp_server.domain.exceptions import DomainError, ManifestSchemaError

        assert issubclass(ManifestSchemaError, DomainError)

    def test_manifest_not_found_error_inherits_from_domain_error(self) -> None:
        from mcp_server.domain.exceptions import DomainError, ManifestNotFoundError

        assert issubclass(ManifestNotFoundError, DomainError)

    def test_manifest_permission_error_inherits_from_domain_error(self) -> None:
        from mcp_server.domain.exceptions import DomainError, ManifestPermissionError

        assert issubclass(ManifestPermissionError, DomainError)

    def test_gitleaks_binary_missing_error_inherits_from_domain_error(self) -> None:
        from mcp_server.domain.exceptions import DomainError, GitleaksBinaryMissingError

        assert issubclass(GitleaksBinaryMissingError, DomainError)


class TestGeminiExceptionHierarchy:
    """``GeminiError`` has two subclasses — transient and permanent."""

    def test_gemini_transient_error_inherits_from_gemini_error(self) -> None:
        from mcp_server.domain.exceptions import GeminiError, GeminiTransientError

        assert issubclass(GeminiTransientError, GeminiError)

    def test_gemini_permanent_error_inherits_from_gemini_error(self) -> None:
        from mcp_server.domain.exceptions import GeminiError, GeminiPermanentError

        assert issubclass(GeminiPermanentError, GeminiError)

    def test_gemini_error_inherits_from_mcp_server_error(self) -> None:
        from mcp_server.domain.exceptions import GeminiError, McpServerError

        assert issubclass(GeminiError, McpServerError)

    def test_gemini_transient_error_can_be_raised(self) -> None:
        from mcp_server.domain.exceptions import GeminiTransientError

        with pytest.raises(GeminiTransientError, match="rate limit"):
            raise GeminiTransientError("rate limit exceeded")


class TestVectorStoreExceptionHierarchy:
    """``VectorStoreError`` and friends cover all DB failures."""

    def test_vector_store_error_inherits_from_mcp_server_error(self) -> None:
        from mcp_server.domain.exceptions import McpServerError, VectorStoreError

        assert issubclass(VectorStoreError, McpServerError)

    def test_embedding_dimension_mismatch_error_inherits_from_vector_store_error(self) -> None:
        from mcp_server.domain.exceptions import (
            EmbeddingDimensionMismatchError,
            VectorStoreError,
        )

        assert issubclass(EmbeddingDimensionMismatchError, VectorStoreError)

    def test_schema_error_inherits_from_vector_store_error(self) -> None:
        from mcp_server.domain.exceptions import SchemaError, VectorStoreError

        assert issubclass(SchemaError, VectorStoreError)


class TestPreindexExceptionAndExitCode:
    """``PreindexError`` and ``PreindexExitCode`` cover the CLI failure mode."""

    def test_preindex_error_inherits_from_mcp_server_error(self) -> None:
        from mcp_server.domain.exceptions import McpServerError, PreindexError

        assert issubclass(PreindexError, McpServerError)

    def test_preindex_exit_code_is_an_enum(self) -> None:
        from mcp_server.domain.exceptions import PreindexExitCode

        assert isinstance(PreindexExitCode, type(enum.Enum()))

    def test_preindex_exit_code_ok_value(self) -> None:
        from mcp_server.domain.exceptions import PreindexExitCode

        assert PreindexExitCode.OK.value == 0

    def test_preindex_exit_code_manifest_error_value(self) -> None:
        from mcp_server.domain.exceptions import PreindexExitCode

        assert PreindexExitCode.MANIFEST_ERROR.value == 2

    def test_preindex_exit_code_gitleaks_error_value(self) -> None:
        from mcp_server.domain.exceptions import PreindexExitCode

        assert PreindexExitCode.GITLEAKS_ERROR.value == 3

    def test_preindex_exit_code_gemini_error_value(self) -> None:
        from mcp_server.domain.exceptions import PreindexExitCode

        assert PreindexExitCode.GEMINI_ERROR.value == 4

    def test_preindex_exit_code_db_error_value(self) -> None:
        from mcp_server.domain.exceptions import PreindexExitCode

        assert PreindexExitCode.DB_ERROR.value == 5


class TestManifestProjectNotFoundError:
    """``ManifestProjectNotFoundError`` is the PR3 manifest helper error."""

    def test_manifest_project_not_found_error_inherits_from_domain_error(self) -> None:
        from mcp_server.domain.exceptions import (
            DomainError,
            ManifestProjectNotFoundError,
        )

        assert issubclass(ManifestProjectNotFoundError, DomainError)


class TestExceptionCatchingContract:
    """Catching ``McpServerError`` covers every adapter-friendly failure."""

    def test_catching_mcp_server_error_catches_everything(self) -> None:
        from mcp_server.domain.exceptions import (
            GeminiPermanentError,
            GeminiTransientError,
            GitleaksBinaryMissingError,
            ManifestNotFoundError,
            ManifestSchemaError,
            McpServerError,
            PreindexError,
            VectorStoreError,
        )

        # All defined domain exceptions MUST be catchable as McpServerError.
        exceptions = [
            GeminiPermanentError("test"),
            GeminiTransientError("test"),
            GitleaksBinaryMissingError("test"),
            ManifestNotFoundError("test"),
            ManifestSchemaError("test"),
            PreindexError("test"),
            VectorStoreError("test"),
        ]
        for exc in exceptions:
            try:
                raise exc
            except McpServerError as caught:
                assert caught is exc
