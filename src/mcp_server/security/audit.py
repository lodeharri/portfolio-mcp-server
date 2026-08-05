"""Structlog-backed audit logger — Layer 5 of the 5-layer security model.

Emits one JSON line per event to stdout. Every line contains at
minimum:

* ``event`` — the event name (e.g. ``"secret.blocked"``).
* ``level`` — ``"info"`` or ``"warning"``.
* ``timestamp`` — ISO-8601 UTC.
* ``**fields`` — caller-provided free-form fields (``source``,
  ``pattern``, ``client_ip``, etc.).

The ``source`` field is sanitized through :class:`OutputSanitizer`
before serialization (threat-matrix row "Secret log leak via audit
JSON"): a token-shaped ``source`` string appears as ``[REDACTED]`` in
the emitted JSON.

The five documented event types:

* ``secret.blocked`` — gitleaks flagged a chunk as high confidence.
* ``secret.flagged`` — gitleaks flagged a chunk as medium confidence.
* ``rate_limit.exceeded`` — a request exceeded the configured limit.
* ``tool.invoked`` — an MCP tool was invoked.
* ``output.redacted`` — output sanitizer replaced one or more secrets.

Configuration is module-level so structlog's processor chain is
consistent across all audit events in the process lifetime. The
composition root holds a single :class:`AuditLogger` instance and
shares it with the gitleaks scanner, slowapi limiter, and (in PR3) the
preindex use case.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from typing import Any

import structlog

from mcp_server.security.output_sanitizer import OutputSanitizer

# ---------------------------------------------------------------------------
# Module-level structlog configuration
# ---------------------------------------------------------------------------


def _add_iso_timestamp(_logger: Any, _method_name: str, event_dict: dict) -> dict:
    """Inject an ISO-8601 UTC timestamp into the event dict."""
    event_dict.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    return event_dict


# Configure structlog once at import time. The output stream is
# stdout so Docker (and `docker logs`) captures every audit line.
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        _add_iso_timestamp,
        structlog.processors.JSONRenderer(sort_keys=True),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
    cache_logger_on_first_use=True,
)


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


class AuditLogger:
    """JSON-line audit logger backed by structlog.

    The constructor is cheap (just builds an :class:`OutputSanitizer`
    for ``source`` field sanitization and binds the structlog
    ``get_logger()`` instance). Two :class:`AuditLogger` instances
    share the same structlog configuration so events emitted from
    either are indistinguishable on the wire.
    """

    def __init__(self) -> None:
        self._sanitizer = OutputSanitizer()
        self._log = structlog.get_logger("mcp_server.audit")

    def info(self, event: str, **fields: Any) -> None:
        """Emit an INFO-level audit event.

        Args:
            event: Event name (e.g. ``"tool.invoked"``).
            **fields: Free-form structured fields (``tool``, ``query``,
                ``client_ip``, etc.).
        """
        self._emit("info", event, **fields)

    def warn(self, event: str, **fields: Any) -> None:
        """Emit a WARNING-level audit event.

        Args:
            event: Event name (e.g. ``"secret.blocked"``,
                ``"rate_limit.exceeded"``).
            **fields: Free-form structured fields.
        """
        self._emit("warning", event, **fields)

    def _emit(self, level: str, event: str, **fields: Any) -> None:
        """Render and emit a single audit line.

        The ``source`` field (when present) is sanitized through
        :class:`OutputSanitizer` so token-shaped strings appear as
        ``[REDACTED]`` in the emitted JSON.
        """
        sanitized = dict(fields)
        if "source" in sanitized and isinstance(sanitized["source"], str):
            sanitized["source"] = self._sanitizer.sanitize(
                sanitized["source"], source="audit-source-field"
            ).redacted_text

        bind = self._log.bind(**sanitized) if sanitized else self._log
        method = getattr(bind, level)
        method(event)


__all__ = ["AuditLogger"]
