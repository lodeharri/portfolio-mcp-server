"""Unit tests for ``interfaces/mcp/tool_errors.translate_tool_error``.

Covers the mapping table from
``openspec/changes/002-mcp-tools/design/adrs/002-tool-error-translation.md``.

FastMCP 3.4.6 limitation
------------------------

The ``fastmcp.exceptions.ToolError`` class in 3.4.6 only carries a
message; the per-tool JSON-RPC code is fixed to ``-32603`` by the
FastMCP transport layer. We therefore test that the **message** is
authored per the mapping table — the message IS the discriminator
on the wire.

The mapping is:

* ``ManifestProjectNotFoundError`` -> "project_id '<id>' not declared in manifest"
* ``ValueError`` (input)            -> "<authored message>" (echoed verbatim)
* ``FileNotFoundError``             -> "referenced file not found" (no path leak)
* ``GeminiTransientError``          -> "service temporarily unavailable, retry later"
* ``GeminiPermanentError``          -> "service rejected the request"
* ``EmbeddingDimensionMismatchError``-> "index dim mismatch — rebuild index"
* ``DomainError`` / ``McpServerError`` catch-all -> "internal error"
* ``TypeError`` / ``AttributeError`` / ``KeyError`` -> re-raised
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

# ---------------------------------------------------------------------------
# Mapped domain exceptions
# ---------------------------------------------------------------------------


class TestMappedDomainErrors:
    """Each known domain exception maps to a specific authored message."""

    def test_manifest_project_not_found_echoes_id(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = ManifestProjectNotFoundError("project 'foo' not declared in manifest")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        # The translated message echoes the project_id for diagnosability
        # (this message is authored by us, not raw SDK text).
        assert "foo" in str(result)

    def test_value_error_is_echoed(self) -> None:
        """Empty / whitespace-only query, top_k>50, etc."""
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = ValueError("query must be a non-empty string")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        # Echo the validation message — we authored it.
        assert "non-empty" in str(result)

    def test_file_not_found_does_not_leak_path(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = FileNotFoundError("ADR at /missing.md")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        # The translated message MUST be a fixed authored string;
        # the raw path from the OSError leaks filesystem structure.
        assert "/missing.md" not in str(result)
        assert "not found" in str(result).lower()

    def test_gemini_transient_error_is_authored(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = GeminiTransientError("429 rate limit exhausted")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        # Authored message — no raw SDK fragment leaks.
        assert "429" not in str(result)
        assert "temporarily" in str(result).lower()

    def test_gemini_permanent_error_is_authored(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = GeminiPermanentError("400 bad request: invalid api key")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        # The raw "invalid api key" phrase MUST NOT leak.
        assert "invalid api key" not in str(result).lower()
        assert "rejected" in str(result).lower()

    def test_embedding_dimension_mismatch_is_authored(self) -> None:
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        exc = EmbeddingDimensionMismatchError("query vector dim 1024 != 768")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        # Authored message: the literal "dim 1024 != 768" is fine but
        # the helper writes a clearer message.
        assert "dim" in str(result).lower()
        assert "rebuild" in str(result).lower()

    def test_generic_domain_error_is_internal_error(self) -> None:
        """Any other :class:`DomainError` subclass defaults to ``internal error``."""
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        class CustomDomainError(DomainError):
            pass

        exc = CustomDomainError("something domain-specific")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        # Catch-all: never leak the original domain message.
        assert "something domain-specific" not in str(result)
        assert "internal error" in str(result).lower()

    def test_mcp_server_error_is_internal_error(self) -> None:
        """Project-wide base maps to internal error (defensive default)."""
        from mcp_server.interfaces.mcp.tool_errors import translate_tool_error

        class CustomMcpError(McpServerError):
            pass

        exc = CustomMcpError("infrastructure blew up")
        result = translate_tool_error(exc)

        assert isinstance(result, ToolError)
        assert "infrastructure blew up" not in str(result)
        assert "internal error" in str(result).lower()


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
        # domain case to give the correct message.
        result = translate_tool_error(GeminiTransientError("oops"))
        assert isinstance(result, ToolError)
        # Authored message (not the raw "oops")
        assert "oops" not in str(result)
        assert "temporarily" in str(result).lower()

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
