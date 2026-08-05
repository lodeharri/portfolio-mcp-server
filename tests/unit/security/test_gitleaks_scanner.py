"""Tests for ``src/mcp_server/security/gitleaks_scanner.py``.

The :class:`GitleaksScanner` is a subprocess wrapper that invokes
``gitleaks detect --no-git --source <tmpdir>`` on each chunk and maps
the exit code to a :class:`ScanVerdict`:

* exit 0 → ``CLEAN`` (no findings)
* exit 1 → ``BLOCKED`` (high-confidence finding — refuse to embed)
* exit 2 → ``FLAGGED`` (medium-confidence finding — embed with flag)
* other → ``BLOCKED`` (fail-closed on unknown exit codes)

Tests are RED until the adapter exists. They mock
``subprocess.run`` via ``unittest.mock`` so the scanner can be exercised
without a real ``gitleaks`` binary.

Threat-matrix coverage
----------------------

* "Subprocess exec (gitleaks)" — content is passed via a tmpdir file,
  not argv, so ``;`` / ``&`` injection is impossible. Tests assert the
  scanner uses ``shell=False`` and ``check=False``.
* "gitleaks binary missing fails closed" — when the binary is not on
  $PATH the scanner raises :class:`GitleaksBinaryMissingError`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from mcp_server.application.ports.secret_scanner import ScanVerdict
from mcp_server.domain.exceptions import GitleaksBinaryMissingError


def _completed_process(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    """Build a fake ``CompletedProcess`` like ``subprocess.run`` would return."""
    return subprocess.CompletedProcess(
        args=["gitleaks", "detect", "--no-git"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestGitleaksScannerExitCodeMapping:
    """``scan()`` maps gitleaks exit codes to ``ScanVerdict`` per design.md."""

    def test_exit_0_returns_clean(self, tmp_path: Path) -> None:
        from mcp_server.security.gitleaks_scanner import GitleaksScanner

        scanner = GitleaksScanner()
        with (
            patch("shutil.which", return_value="/usr/local/bin/gitleaks"),
            patch("subprocess.run", return_value=_completed_process(0)) as mock_run,
        ):
            verdict = scanner.scan("hello world", source=str(tmp_path / "x.py"))
        assert verdict == ScanVerdict.CLEAN
        mock_run.assert_called_once()

    def test_exit_1_returns_blocked(self, tmp_path: Path) -> None:
        from mcp_server.security.gitleaks_scanner import GitleaksScanner

        scanner = GitleaksScanner()
        with (
            patch("shutil.which", return_value="/usr/local/bin/gitleaks"),
            patch("subprocess.run", return_value=_completed_process(1, stdout='{"findings": [...]}')),
        ):
            verdict = scanner.scan("AKIA1234567890ABCDEF", source=str(tmp_path / "x.py"))
        assert verdict == ScanVerdict.BLOCKED

    def test_exit_2_returns_flagged(self, tmp_path: Path) -> None:
        """Exit 2 = medium confidence (future gitleaks versions)."""
        from mcp_server.security.gitleaks_scanner import GitleaksScanner

        scanner = GitleaksScanner()
        with (
            patch("shutil.which", return_value="/usr/local/bin/gitleaks"),
            patch("subprocess.run", return_value=_completed_process(2)),
        ):
            verdict = scanner.scan("maybe-a-secret", source=str(tmp_path / "x.py"))
        assert verdict == ScanVerdict.FLAGGED

    def test_unknown_exit_returns_blocked_fail_closed(self, tmp_path: Path) -> None:
        """Unknown exit codes MUST map to BLOCKED (fail-closed)."""
        from mcp_server.security.gitleaks_scanner import GitleaksScanner

        scanner = GitleaksScanner()
        with (
            patch("shutil.which", return_value="/usr/local/bin/gitleaks"),
            patch("subprocess.run", return_value=_completed_process(99)),
        ):
            verdict = scanner.scan("mystery", source=str(tmp_path / "x.py"))
        assert verdict == ScanVerdict.BLOCKED


class TestGitleaksScannerSubprocessSafety:
    """``scan()`` invokes subprocess safely (threat-matrix row "Subprocess exec")."""

    def test_subprocess_called_with_shell_false(self, tmp_path: Path) -> None:
        """``shell=False`` so argv injection (``;``, ``&``) is impossible."""
        from mcp_server.security.gitleaks_scanner import GitleaksScanner

        scanner = GitleaksScanner()
        with (
            patch("shutil.which", return_value="/usr/local/bin/gitleaks"),
            patch("subprocess.run", return_value=_completed_process(0)) as mock_run,
        ):
            scanner.scan("safe content", source=str(tmp_path / "x.py"))
        assert mock_run.call_args.kwargs.get("shell") is False

    def test_subprocess_called_with_check_false(self, tmp_path: Path) -> None:
        """``check=False`` so non-zero exit codes don't raise."""
        from mcp_server.security.gitleaks_scanner import GitleaksScanner

        scanner = GitleaksScanner()
        with (
            patch("shutil.which", return_value="/usr/local/bin/gitleaks"),
            patch("subprocess.run", return_value=_completed_process(1)) as mock_run,
        ):
            scanner.scan("AKIA1234567890ABCDEF", source=str(tmp_path / "x.py"))
        assert mock_run.call_args.kwargs.get("check") is False

    def test_content_written_to_tmp_dir_not_argv(self, tmp_path: Path) -> None:
        """The chunk content goes into a tmpdir file, NOT into argv."""
        from mcp_server.security.gitleaks_scanner import GitleaksScanner

        scanner = GitleaksScanner()
        malicious = '"; rm -rf /; echo "'
        with (
            patch("shutil.which", return_value="/usr/local/bin/gitleaks"),
            patch("subprocess.run", return_value=_completed_process(0)) as mock_run,
        ):
            scanner.scan(malicious, source=str(tmp_path / "x.py"))
        # The content MUST NOT appear as a substring of the argv list.
        argv = mock_run.call_args.args[0] if mock_run.call_args.args else mock_run.call_args.kwargs.get("args", [])
        joined = " ".join(str(a) for a in argv)
        assert "rm -rf" not in joined
        assert malicious not in joined


class TestGitleaksScannerBinaryMissing:
    """``scan()`` raises ``GitleaksBinaryMissingError`` when gitleaks is absent."""

    def test_missing_binary_raises_error(self, tmp_path: Path) -> None:
        from mcp_server.security.gitleaks_scanner import GitleaksScanner

        scanner = GitleaksScanner()
        # Make `which gitleaks` raise FileNotFoundError to simulate the
        # binary being missing. The scanner MUST surface this as the
        # domain error rather than letting it propagate.
        with patch("shutil.which", return_value=None):
            with pytest.raises(GitleaksBinaryMissingError):
                scanner.scan("anything", source=str(tmp_path / "x.py"))


class TestGitleaksScannerPortConformance:
    """``GitleaksScanner`` satisfies ``SecretScannerPort`` structurally."""

    def test_scanner_satisfies_secret_scanner_port(self, tmp_path: Path) -> None:
        from mcp_server.application.ports.secret_scanner import SecretScannerPort
        from mcp_server.security.gitleaks_scanner import GitleaksScanner

        scanner = GitleaksScanner()
        assert isinstance(scanner, SecretScannerPort)


class TestFindGitleaksBinary:
    """``find_gitleaks_binary()`` locates the binary on $PATH."""

    def test_returns_path_when_on_path(self) -> None:
        from mcp_server.security.gitleaks_scanner import find_gitleaks_binary

        with patch("shutil.which", return_value="/usr/local/bin/gitleaks"):
            path = find_gitleaks_binary()
        assert path == Path("/usr/local/bin/gitleaks")

    def test_returns_none_when_missing(self) -> None:
        from mcp_server.security.gitleaks_scanner import find_gitleaks_binary

        with patch("shutil.which", return_value=None):
            assert find_gitleaks_binary() is None