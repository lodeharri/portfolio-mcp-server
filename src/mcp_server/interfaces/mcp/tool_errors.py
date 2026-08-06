"""``translate_tool_error`` — central DomainError -> FastMCP ToolError mapping.

Six ``@mcp.tool`` wrappers + 1 agent use case call into ONE helper so
the message discipline (never raw SDK strings) is enforced in one
place. See
``openspec/changes/002-mcp-tools/design/adrs/002-tool-error-translation.md``
for the full rationale.

FastMCP 3.4.6 limitation
------------------------

The ``fastmcp.exceptions.ToolError`` class in 3.4.6 only accepts
``(message, log_level=ERROR)`` — it does NOT carry a per-tool JSON-RPC
code. FastMCP's transport layer always serializes a tool exception as
``is_error: true`` with the message; the JSON-RPC code is determined
at the transport level (the MCP protocol reserves ``-32602`` for
``invalid params`` and ``-32603`` for ``internal error``, but the
per-tool error path doesn't have a knob for these).

We therefore map each domain exception to a distinct **authored
message** that identifies the error class. The message IS the
discriminator — clients can match on substring (``"service
temporarily unavailable"`` vs ``"referenced file not found"``).

This is documented as a follow-up boundary in ADR-002: if/when
FastMCP exposes a per-tool code, the mapping table here can be
extended in one place to set it. Until then, the helper's job is
to guarantee the message is safe to expose to the MCP client.

Message mapping
---------------

================================  =========================================
Exception                         Authored message
================================  =========================================
ManifestProjectNotFoundError      ``project_id "<id>" not declared in
                                        manifest`` (echoes the id for
                                        diagnosability)
ValueError (input validation)     ``<authored message>`` (echoed verbatim;
                                        we wrote it, no SDK leak)
FileNotFoundError                 ``referenced file not found`` (raw path
                                        is OSError text — never echoed)
GeminiTransientError              ``service temporarily unavailable, retry
                                        later``
GeminiPermanentError              ``service rejected the request``
EmbeddingDimensionMismatchError   ``index dim mismatch — rebuild index``
DomainError (catch-all)           ``internal error``
McpServerError (catch-all)        ``internal error``
TypeError/AttributeError/KeyError re-raised (programming bug, not domain)
================================  =========================================

Subclass precedence
-------------------

``isinstance`` checks run in order from most specific to most
generic. ``GeminiTransientError`` is also a ``DomainError`` — the
transient check MUST come first, otherwise the generic domain
mapping would shadow it. Same for ``ManifestProjectNotFoundError``
vs ``ManifestError`` vs ``DomainError``.

The helper does NOT call ``sanitize(...)`` on the translated message
because every message is authored in this module — none can carry
secrets by construction. The use case boundary (where secrets can
appear) handles its own sanitization via ``OutputSanitizer``.
"""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from mcp_server.domain.exceptions import (
    DomainError,
    EmbeddingDimensionMismatchError,
    GeminiPermanentError,
    GeminiTransientError,
    ManifestProjectNotFoundError,
    McpServerError,
    RateLimitExceeded,
)

__all__ = [
    "JSON_RPC_INTERNAL_ERROR",
    "JSON_RPC_INVALID_PARAMS",
    "translate_tool_error",
]


# JSON-RPC 2.0 standard error codes (per the MCP transport spec).
# Documented here so future FastMCP versions that DO support a per-tool
# code can pick the right one from the translate_tool_error mapping
# table.
JSON_RPC_INVALID_PARAMS: int = -32602
JSON_RPC_INTERNAL_ERROR: int = -32603


def translate_tool_error(exc: BaseException) -> ToolError:
    """Map a domain exception to a :class:`fastmcp.exceptions.ToolError`.

    Args:
        exc: The exception raised inside an ``@mcp.tool`` wrapper
            (or the agent use case). Subclasses are matched in order
            from most specific to most generic.

    Returns:
        A :class:`ToolError` carrying an authored message. The wrapper
        can ``raise`` it directly — FastMCP serializes it into the
        JSON-RPC error response with code ``-32603`` (INTERNAL_ERROR)
        and the message as ``text``.

    Raises:
        BaseException: re-raises programming errors (``TypeError``,
            ``AttributeError``, ``KeyError``, …). The intent is that
            unknown exceptions bubble up so FastMCP surfaces a 500
            and the maintainer notices — rather than being silently
            mapped to a generic error.
    """
    # --- Specific exception types (most specific first) ---

    if isinstance(exc, ManifestProjectNotFoundError):
        # Echo the project_id so the recruiter sees which one was wrong.
        # The message string is still "authored" by us (we wrote the
        # ManifestProjectNotFoundError message) so no leak risk.
        return ToolError(str(exc))

    if isinstance(exc, ValueError):
        # Input validation: empty query, top_k > 50, etc. We authored
        # the message ourselves so echoing it is safe.
        return ToolError(str(exc))

    if isinstance(exc, FileNotFoundError):
        # ADR/README/SVG missing on disk. Path is OSError text — never
        # echo it (it leaks filesystem structure).
        return ToolError("referenced file not found")

    if isinstance(exc, GeminiTransientError):
        # Authored message: no raw "429" or SDK text reaches the client.
        return ToolError("service temporarily unavailable, retry later")

    if isinstance(exc, GeminiPermanentError):
        # Authored message: no raw "invalid api key" or model name leak.
        return ToolError("service rejected the request")

    if isinstance(exc, EmbeddingDimensionMismatchError):
        # Authored message: a "dim 1024 != 768" leak is fine but the
        # message is more useful to the recruiter.
        return ToolError("index dim mismatch — rebuild index")

    if isinstance(exc, RateLimitExceeded):
        # Per 002-mcp-tools PR3 spec: application-layer rate-limit
        # rejection from ``ask_portfolio`` → JSON-RPC internal error.
        # Authored message: no ``client_ip`` or counter value leaks.
        return ToolError("rate limit exceeded")

    # --- Catch-all buckets (defensive defaults) ---

    if isinstance(exc, DomainError):
        # Unknown domain exception — keep the message but never raw SDK.
        return ToolError("internal error")

    if isinstance(exc, McpServerError):
        # Project-wide base — defensive default.
        return ToolError("internal error")

    # --- Programming errors re-raised ---

    # ``TypeError``, ``AttributeError``, ``KeyError``, ``NameError`` etc.
    # are NOT domain errors — they're bugs in the wrapper code. Let
    # them bubble so FastMCP surfaces a 500 and the maintainer notices.
    raise exc
