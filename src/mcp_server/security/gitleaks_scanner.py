"""Gitleaks-backed secret scanner — implements :class:`SecretScannerPort`.

Layer 2 of the 5-layer security model. Wraps the ``gitleaks`` binary
via ``subprocess.run`` and translates its exit code to
:class:`ScanVerdict`:

* exit 0 → ``CLEAN`` (no findings)
* exit 1 → ``BLOCKED`` (high-confidence finding)
* exit 2 → ``FLAGGED`` (medium-confidence finding, future gitleaks)
* other / timeout / error → ``BLOCKED`` (fail-closed)

Construction is eager (ADR-001) but the actual subprocess call is lazy
— it happens on each :meth:`GitleaksScanner.scan` invocation. The
composition root constructs one scanner per app, and the preindex
pipeline calls ``scan`` once per chunk.

Threat-matrix coverage
----------------------

* "Subprocess exec (gitleaks)" — content is written to a tmpdir file
  and ``--source <tmpdir>`` is passed via argv. ``shell=False`` and
  ``check=False`` are enforced. The chunk content is never on the argv
  list, so ``;`` / ``&`` injection is impossible.
* "gitleaks binary missing fails closed" — when the binary is not on
  ``$PATH`` (and ``~/.local/bin/gitleaks`` is also absent),
  :meth:`GitleaksScanner.scan` raises :class:`GitleaksBinaryMissingError`
  and the preindex pipeline aborts.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from mcp_server.application.ports.secret_scanner import ScanVerdict
from mcp_server.domain.exceptions import GitleaksBinaryMissingError

# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------


def find_gitleaks_binary() -> Path | None:
    """Locate the ``gitleaks`` binary on the system.

    Resolution order:

    1. ``shutil.which("gitleaks")`` — search ``$PATH``.
    2. ``~/.local/bin/gitleaks`` — common install location for tools
       installed via ``curl ... | sh`` (the project's Dockerfile uses
       this path).

    Returns:
        Absolute path to the binary, or ``None`` when not found.
    """
    on_path = shutil.which("gitleaks")
    if on_path is not None:
        return Path(on_path)
    user_local = Path.home() / ".local" / "bin" / "gitleaks"
    if user_local.is_file() and shutil.which(str(user_local)) is not None:
        return user_local
    return None


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class GitleaksScanner:
    """Subprocess wrapper implementing :class:`SecretScannerPort`.

    The audit logger is OPTIONAL — when provided, the scanner logs a
    ``secret.blocked`` / ``secret.flagged`` event for non-clean verdicts.
    The audit logger is injected at construction time (composition root)
    so the scanner can be unit-tested without it.
    """

    def __init__(self, audit: object | None = None) -> None:
        """Store the optional audit logger.

        Args:
            audit: Optional audit logger with ``.warn(event, **fields)``.
                When ``None``, scan events are not logged.
        """
        self._audit = audit

    def scan(self, content: str, source: str) -> ScanVerdict:
        """Scan ``content`` for secrets via gitleaks.

        Args:
            content: Chunk text to scan.
            source: File path or identifier. Passed through to gitleaks
                for richer audit messages. The scanner does NOT echo this
                through the audit log unfiltered; the audit logger is
                responsible for sanitization (see ``AuditLogger``).

        Returns:
            :class:`ScanVerdict` mapped from the gitleaks exit code.

        Raises:
            GitleaksBinaryMissingError: when the gitleaks binary cannot
                be located on the system. The preindex pipeline treats
                this as a hard abort (fail-closed).
        """
        binary = find_gitleaks_binary()
        if binary is None:
            raise GitleaksBinaryMissingError(
                "gitleaks binary not found on $PATH or ~/.local/bin/gitleaks; "
                "install gitleaks or set GITLEAKS_BINARY env var"
            )

        # Write the chunk into a unique tmpdir so gitleaks can scan it
        # without our argv leaking any chunk content.
        with tempfile.TemporaryDirectory() as tmpdir:
            chunk_path = Path(tmpdir) / "chunk.txt"
            chunk_path.write_text(content, encoding="utf-8")

            try:
                result = subprocess.run(  # noqa: S603 — gitleaks binary is fixed, content via tmpdir file
                    [str(binary), "detect", "--no-git", "--source", tmpdir],
                    capture_output=True,
                    text=True,
                    check=False,
                    shell=False,  # threat-matrix: no shell, no argv injection
                    cwd=tmpdir,
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                # Fail-closed on timeout.
                self._emit_audit("secret.timeout", source=source)
                return ScanVerdict.BLOCKED

        verdict = self._map_exit_code(result.returncode)
        if verdict is ScanVerdict.BLOCKED and result.returncode != 1:
            self._emit_audit("secret.blocked", source=source, exit_code=result.returncode)
        elif verdict is ScanVerdict.BLOCKED:
            self._emit_audit("secret.blocked", source=source, exit_code=1)
        elif verdict is ScanVerdict.FLAGGED:
            self._emit_audit("secret.flagged", source=source, exit_code=2)
        return verdict

    @staticmethod
    def _map_exit_code(returncode: int) -> ScanVerdict:
        """Translate gitleaks' exit code to a :class:`ScanVerdict`.

        Mapping per ``openspec/changes/001-bootstrap/design.md``
        (Capability 3 → Secret-Redaction Flow).
        """
        if returncode == 0:
            return ScanVerdict.CLEAN
        if returncode == 1:
            return ScanVerdict.BLOCKED
        if returncode == 2:
            return ScanVerdict.FLAGGED
        # Any other code is treated as BLOCKED (fail-closed).
        return ScanVerdict.BLOCKED

    def _emit_audit(self, event: str, **fields: object) -> None:
        """Emit an audit event if a logger was injected at construction."""
        if self._audit is None:
            return
        emit = getattr(self._audit, "warn", None)
        if callable(emit):
            emit(event, **fields)


__all__ = ["GitleaksScanner", "find_gitleaks_binary"]
