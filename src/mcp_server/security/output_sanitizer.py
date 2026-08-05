"""Output sanitizer — Layer 3 of the 5-layer security model.

Replaces every match of five ``SecretPattern`` regexes with the literal
string ``[REDACTED]`` and returns both the redacted text and the list
of :class:`RedactionIncident` records for the audit log.

The five patterns (per ``openspec/changes/001-bootstrap/specs/security-layers.md``
→ "Output Sanitizer Redacts Known Patterns"):

* ``AWS`` — ``AKIA[0-9A-Z]{16}``
* ``GITHUB`` — ``ghp_[a-zA-Z0-9]{36}``
* ``OPENAI`` — ``sk-[a-zA-Z0-9]{48}``
* ``GEMINI`` — ``AIza[0-9A-Za-z_-]{35}``
* ``GENERIC`` — ``(api[_-]?key|secret|password|token)\\s*[:=]\\s*['\"]?[\\w-]+``

Additionally, when an audit logger is injected at construction time,
the sanitizer emits an ``output.redacted`` event for every successful
``sanitize`` that returns at least one incident. This satisfies the
Layer 5 audit contract at the Layer 3 boundary so T2.13 HTTP middleware
(PR3) and the preindex use case don't need to repeat the audit call.

Thread-safety: the compiled regexes live at MODULE LEVEL. Two sanitizer
instances share the same compiled patterns, so concurrent sanitization
across threads is safe (no per-instance mutable state).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Patterns enum + module-level compiled regexes
# ---------------------------------------------------------------------------


class SecretPattern(Enum):
    """The five secret patterns the sanitizer redacts.

    Values are lowercase strings so they serialize cleanly into the
    audit log (structlog JSON).
    """

    AWS = "aws"
    GITHUB = "github"
    OPENAI = "openai"
    GEMINI = "gemini"
    GENERIC = "generic"


# Module-level compiled regexes — shared across all OutputSanitizer
# instances. This is the thread-safety guarantee: regex matching is
# read-only once compiled.
_PATTERN_TABLE: list[tuple[SecretPattern, re.Pattern[str]]] = [
    (SecretPattern.AWS, re.compile(r"AKIA[0-9A-Z]{16}")),
    (SecretPattern.GITHUB, re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    (SecretPattern.OPENAI, re.compile(r"sk-[a-zA-Z0-9]{48}")),
    (SecretPattern.GEMINI, re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    (
        SecretPattern.GENERIC,
        re.compile(r"(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[\w-]+"),
    ),
]


# Re-exported for tests that assert module-level compiled constants.
PATTERN_AWS = _PATTERN_TABLE[0][1]
PATTERN_GITHUB = _PATTERN_TABLE[1][1]
PATTERN_OPENAI = _PATTERN_TABLE[2][1]
PATTERN_GEMINI = _PATTERN_TABLE[3][1]
PATTERN_GENERIC = _PATTERN_TABLE[4][1]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class RedactionIncident(BaseModel):
    """A single redaction event, recorded for the audit log."""

    pattern: SecretPattern
    start: int
    end: int
    source: str
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SanitizedOutput(BaseModel):
    """Result of sanitizing a piece of text.

    ``redacted_text`` is the input with every secret replaced by
    ``[REDACTED]``. ``incidents`` records each replacement with the
    matched pattern, character offsets, and source label.
    """

    redacted_text: str
    incidents: list[RedactionIncident] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Sanitizer
# ---------------------------------------------------------------------------


_REDACTED_PLACEHOLDER = "[REDACTED]"


class _AuditEmitter(Protocol):
    """Protocol for the audit logger the sanitizer talks to.

    Kept local to this module so the sanitizer does not import the
    :class:`AuditLogger` from ``mcp_server.security.audit`` (which would
    create a circular dependency: ``audit`` already imports the
    sanitizer for its ``source`` field sanitization).
    """

    def warn(self, event: str, **fields: Any) -> None: ...


class OutputSanitizer:
    """Replace every ``SecretPattern`` match in ``text`` with ``[REDACTED]``.

    Construction is cheap — the compiled regexes are module-level, so
    instantiation is just an attribute assignment. Two instances are
    fully interchangeable; both can be used concurrently from
    different threads.

    The optional ``audit`` argument wires the Layer 5 audit emission
    required by the spec ("Every redaction, every blocked scan, and
    every rate-limit hit MUST be logged"). When injected, every
    ``sanitize`` call that returns at least one incident raises
    ``audit.warn("output.redacted", ...)`` with the matched pattern
    names, the redaction count, and the source label. When ``audit``
    is ``None`` (e.g. unit tests, CLI helpers) the sanitizer still
    redacts but stays silent — the redaction itself is the security
    boundary, the audit is the observability layer.
    """

    def __init__(self, audit: _AuditEmitter | None = None) -> None:
        self._audit = audit

    def sanitize(self, text: str, source: str) -> SanitizedOutput:
        """Redact secrets from ``text``.

        Args:
            text: Arbitrary text (tool output, HTTP response body, etc.).
            source: Label for the audit log — typically the tool name or
                HTTP route. Echoed verbatim in the incident record; the
                audit logger is responsible for sanitizing the JSON
                field downstream.

        Returns:
            :class:`SanitizedOutput` with the redacted text and the
            list of incidents. Clean text returns the input unchanged
            with an empty ``incidents`` list.
        """
        incidents: list[RedactionIncident] = []
        redacted = text

        for pattern, regex in _PATTERN_TABLE:
            # Use re.sub with a function so we can record each incident.
            def _sub_and_record(
                match: re.Match[str],
                _pattern: SecretPattern = pattern,
            ) -> str:
                incidents.append(
                    RedactionIncident(
                        pattern=_pattern,
                        start=match.start(),
                        end=match.end(),
                        source=source,
                    )
                )
                return _REDACTED_PLACEHOLDER

            redacted = regex.sub(_sub_and_record, redacted)

        if incidents and self._audit is not None:
            # Aggregate the matched patterns + count so the audit log
            # doesn't flood on find-heavy payloads. The Layer 5 audit
            # contract is "every redaction is logged"; one event per
            # sanitize call satisfies that contract without N events
            # for N matches.
            unique_patterns = sorted({i.pattern.value for i in incidents})
            self._audit.warn(
                "output.redacted",
                source=source,
                count=len(incidents),
                patterns=",".join(unique_patterns),
            )

        return SanitizedOutput(redacted_text=redacted, incidents=incidents)

    def sanitize_json(self, obj: object, source: str) -> SanitizedOutput:
        """Recursively sanitize every string value in ``obj``.

        Args:
            obj: Arbitrary JSON-serializable structure (dict, list,
                primitive). Non-string scalars are passed through as-is.
            source: Audit-log label.

        Returns:
            :class:`SanitizedOutput` whose ``redacted_text`` is the
            JSON-serialized sanitized payload. ``incidents`` aggregates
            every match found at any depth.
        """
        sanitized, incidents = self._walk(obj, source)
        return SanitizedOutput(
            redacted_text=json.dumps(sanitized, separators=(",", ":")),
            incidents=incidents,
        )

    def _walk(self, obj: object, source: str) -> tuple[object, list[RedactionIncident]]:
        """Recursively walk ``obj``, sanitizing every string value.

        Returns the sanitized structure and the aggregated incidents.
        """
        if isinstance(obj, dict):
            merged: list[RedactionIncident] = []
            new_dict: dict[object, object] = {}
            for key, value in obj.items():
                clean_value, sub_incidents = self._walk(value, source)
                new_dict[key] = clean_value
                merged.extend(sub_incidents)
            return new_dict, merged
        if isinstance(obj, list):
            merged = []
            new_list: list[object] = []
            for value in obj:
                clean_value, sub_incidents = self._walk(value, source)
                new_list.append(clean_value)
                merged.extend(sub_incidents)
            return new_list, merged
        if isinstance(obj, str):
            result = self.sanitize(obj, source)
            return result.redacted_text, list(result.incidents)
        # Scalars (int, float, bool, None) pass through unchanged.
        return obj, []


__all__ = [
    "PATTERN_AWS",
    "PATTERN_GEMINI",
    "PATTERN_GENERIC",
    "PATTERN_GITHUB",
    "PATTERN_OPENAI",
    "OutputSanitizer",
    "RedactionIncident",
    "SanitizedOutput",
    "SecretPattern",
]
