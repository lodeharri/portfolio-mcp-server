"""Secret scanner port — application-layer contract for chunk secret scanning.

Layer 2 of the 5-layer security model. The :class:`ScanVerdict` enum is
the single source of truth for translating gitleaks exit codes into
application-layer decisions:

* ``CLEAN`` — no findings, safe to embed and insert.
* ``FLAGGED`` — medium-confidence finding, insert with ``flagged=True``
  so the audit log records the incident.
* ``BLOCKED`` — high-confidence finding, refuse to embed or insert.

The concrete ``GitleaksScanner`` lives in ``src/mcp_server/security/`` and
maps gitleaks exit codes (0 / 1 / 2) to these verdicts. A separate
adapter location is intentional — security modules may be reused outside
the composition root (e.g. by future ad-hoc CLI tools) without dragging
in the FastAPI app.
"""

from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable


class ScanVerdict(Enum):
    """Outcome of a secret scan.

    Values are lowercase strings so they serialize cleanly in the audit
    log (structlog JSON).
    """

    CLEAN = "clean"
    FLAGGED = "flagged"
    BLOCKED = "blocked"


@runtime_checkable
class SecretScannerPort(Protocol):
    """Contract for any secret-scanning adapter.

    Implementations MUST be fail-closed: any error from the underlying
    scanner (binary missing, malformed output, timeout) maps to
    ``BLOCKED`` rather than ``CLEAN``. The preindex pipeline treats
    ``BLOCKED`` as a hard refusal.
    """

    def scan(self, content: str, source: str) -> ScanVerdict:
        """Scan ``content`` for secrets.

        Args:
            content: Chunk text to scan.
            source: File path or identifier for audit logging. The
                adapter MUST pass this through to the audit log but
                MUST NOT echo secrets from it (see threat-matrix row
                "Secret log leak via audit JSON").

        Returns:
            One of ``ScanVerdict.CLEAN``, ``ScanVerdict.FLAGGED``,
            ``ScanVerdict.BLOCKED``. Errors map to ``BLOCKED``.
        """
        ...


__all__ = ["ScanVerdict", "SecretScannerPort"]
