"""Unit tests for ``interfaces/mcp/tool_errors.translate_tool_error``.

Covers the mapping table from
``openspec/changes/002-mcp-tools/design/adrs/002-tool-error-translation.md``:

* ``ManifestProjectNotFoundError`` -> ``-32602`` invalid params
* ``ValueError`` (input validation) -> ``-32602`` invalid params
* ``FileNotFoundError`` -> ``-32603`` internal error
* ``GeminiTransientError`` -> ``-32603`` internal error
* ``GeminiPermanentError`` -> ``-32603`` internal error
* ``EmbeddingDimensionMismatchError`` -> ``-32603`` internal error
* Generic ``DomainError`` -> ``-32603`` internal error
* ``McpServerError`` (project base) -> ``-32603`` internal error
* Programming errors (``TypeError``, ``AttributeError``) re-raised

The translated message MUST be authored (never the raw ``str(exc)``
for SDK-originated exceptions) so a token-shaped fragment in the
SDK message cannot leak.
"""

from __future__ import annotations

import pytest
from fastmcp.exceptions import ToolError

from mcp_server.domain.exceptions import (
    DomainError,
    EmbeddingDimensionMismatchError,
    GeminiPermanentError,
    GeminiTransientError,
    ManifestProjectNotFoundError,
    McpServerError,
)

JSON_RPC_INVALID_PARAMS = -32602
JSON_RPC_INTERNAL_ERROR = -32603


# ---------------------------------------------------------------------------
# Mapped domain exceptions
# ---------------------------------------------------------------------------


class TestMappedDomainErrors:
    """Each known domain exception maps to a specific JSON-RPC code."""

    def test_manifest_project_not_found_is_invalid_params(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = ManifestProjectNotFoundError("project 'foo' not declared in manifest")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        assert result.code == JSON_RPC_INVALID_PARAMS
        # The translated message echoes the project_id for diagnosability
        # (this message is authored by us, not raw SDK text).
        assert "foo" in str(result)

    def test_value_error_is_invalid_params(self) -> None:
        """Empty / whitespace-only query, top_k>50, etc."""
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = ValueError("query must be a non-empty string")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        assert result.code == JSON_RPC_INVALID_PARAMS
        # Echo the validation message — we authored it.
        assert "non-empty" in str(result)

    def test_file_not_found_is_internal_error(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = FileNotFoundError("ADR at /missing.md")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        assert result.code == JSON_RPC_INTERNAL_ERROR
        # The translated message MUST be a fixed authored string;
        # the raw path from the OSError leaks filesystem structure.
        assert "/missing.md" not in str(result)

    def test_gemini_transient_error_is_internal_error(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = GeminiTransientError("429 rate limit exhausted")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        assert result.code == JSON_RPC_INTERNAL_ERROR
        # Authored message — no raw SDK fragment leaks.
        assert "429" not in str(result)
        assert "rate limit" in str(result).lower() or "temporarily" in str(result).lower()

    def test_gemini_permanent_error_is_internal_error(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = GeminiPermanentError("400 bad request: invalid api key")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        assert result.code == JSON_RPC_INTERNAL_ERROR
        # The raw "invalid api key" phrase MUST NOT leak.
        assert "invalid api key" not in str(result).lower()

    def test_embedding_dimension_mismatch_is_internal_error(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = EmbeddingDimensionMismatchError("query vector dim 1024 != 768")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        assert result.code == JSON_RPC_INTERNAL_ERROR

    def test_generic_domain_error_is_internal_error(self) -> None:
        """Any other :class:`DomainError` subclass defaults to ``-32603``."""
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        class CustomDomainError(DomainError):
            pass

        exc = CustomDomainError("something domain-specific")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        assert result.code == JSON_RPC_INTERNAL_ERROR

    def test_mcp_server_error_is_internal_error(self) -> None:
        """Project-wide base maps to internal error (defensive default)."""
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        class CustomMcpError(McpServerError):
            pass

        exc = CustomMcpError("infrastructure blew up")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        assert result.code == JSON_RPC_INTERNAL_ERROR


# ---------------------------------------------------------------------------
# Programming errors re-raised (not caught)
# ---------------------------------------------------------------------------


class TestProgrammingErrorsReraise:
    """Bugs (``TypeError``, ``AttributeError``) MUST NOT be caught."""

    def test_type_error_reraises(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        with pytest.raises(TypeError):
            translate_tool_error(TypeError("'NoneType' has no attribute 'x'"))

    def test_attribute_error_reraises(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        with pytest.raises(AttributeError):
            translate_tool_error(AttributeError("module 'foo' has no attribute 'bar'"))

    def test_key_error_reraises(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        with pytest.raises(KeyError):
            translate_tool_error(KeyError("missing-key"))


# ---------------------------------------------------------------------------
# Specific subclass relationship: GeminiTransientError < GeminiError < DomainError
# ---------------------------------------------------------------------------


class TestExceptionHierarchy:
    """Subclass relationships MUST be respected by the mapping table."""

    def test_gemini_transient_is_also_a_domain_error(self) -> None:
        """``isinstance`` checks in the helper must use the subclass first."""
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        # ``GeminiTransientError`` is also a ``DomainError`` — the
        # helper must match the transient case BEFORE the generic
        # domain case to give the correct code + message.
        result = translate_tool_error(GeminiTransientError("oops"))
        assert result.code == JSON_RPC_INTERNAL_ERROR
        # Authored message (not the raw "oops")
        assert "oops" not in str(result)

    def test_manifest_project_not_found_is_specific_not_generic_domain(self) -> None:
        """``ManifestProjectNotFoundError`` MUST get its specific message,
        not the generic domain default."""
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        result = translate_tool_error(
            ManifestProjectNotFoundError("project 'bar' not declared")
        )
        # The specific mapping echoes the project id; the generic
        # domain default would say "internal error".
        assert "bar" in str(result)
        assert "internal error" not in str(result).lower()
