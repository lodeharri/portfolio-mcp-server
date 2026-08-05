"""Conformance tests for ``src/mcp_server/application/ports/secret_scanner.py``.

The :class:`SecretScannerPort` Protocol declares the contract a gitleaks-backed
secret scanner must satisfy, along with the :class:`ScanVerdict` enum that
maps gitleaks exit codes to application-layer decisions:

* ``CLEAN`` (``"clean"``) — no findings, safe to embed.
* ``FLAGGED`` (``"flagged"``) — medium-confidence finding, embed with flag.
* ``BLOCKED`` (``"blocked"``) — high-confidence finding, refuse to embed.

Per the orchestrator's PR2 spec, ``SecretScannerPort.scan(content, source)``
returns a :class:`ScanVerdict` — the protocol is the single source of truth
for exit-code translation (Layer 2 of the 5-layer security model).
"""

from __future__ import annotations

import inspect


class TestScanVerdictEnum:
    """``ScanVerdict`` is a string-valued enum with three members."""

    def test_scan_verdict_enum_exists(self) -> None:
        from mcp_server.application.ports.secret_scanner import ScanVerdict

        assert ScanVerdict is not None

    def test_scan_verdict_has_clean(self) -> None:
        from mcp_server.application.ports.secret_scanner import ScanVerdict

        assert hasattr(ScanVerdict, "CLEAN")
        assert ScanVerdict.CLEAN.value == "clean"

    def test_scan_verdict_has_flagged(self) -> None:
        from mcp_server.application.ports.secret_scanner import ScanVerdict

        assert hasattr(ScanVerdict, "FLAGGED")
        assert ScanVerdict.FLAGGED.value == "flagged"

    def test_scan_verdict_has_blocked(self) -> None:
        from mcp_server.application.ports.secret_scanner import ScanVerdict

        assert hasattr(ScanVerdict, "BLOCKED")
        assert ScanVerdict.BLOCKED.value == "blocked"


class TestSecretScannerPortProtocol:
    """``SecretScannerPort`` declares the contract for chunk secret scanning."""

    def test_secret_scanner_port_protocol_exists(self) -> None:
        from mcp_server.application.ports.secret_scanner import SecretScannerPort

        assert SecretScannerPort is not None

    def test_secret_scanner_port_has_scan(self) -> None:
        from mcp_server.application.ports.secret_scanner import SecretScannerPort

        members = dict(inspect.getmembers(SecretScannerPort))
        assert "scan" in members


class TestSecretScannerPortConformance:
    """A class with a ``scan`` method returning ``ScanVerdict`` satisfies the port."""

    def test_fake_scanner_satisfies_protocol(self) -> None:
        from mcp_server.application.ports.secret_scanner import (
            ScanVerdict,
            SecretScannerPort,
        )

        class FakeScanner:
            def scan(self, content: str, source: str) -> ScanVerdict:
                return ScanVerdict.CLEAN

        assert isinstance(FakeScanner(), SecretScannerPort)

    def test_fake_scanner_returns_verdict_values(self) -> None:
        from mcp_server.application.ports.secret_scanner import ScanVerdict

        class FakeScanner:
            def scan(self, content: str, source: str) -> ScanVerdict:
                if "AKIA" in content:
                    return ScanVerdict.BLOCKED
                if "warning" in content:
                    return ScanVerdict.FLAGGED
                return ScanVerdict.CLEAN

        scanner = FakeScanner()
        assert scanner.scan("hello", "x.py") == ScanVerdict.CLEAN
        assert scanner.scan("AKIA1234567890ABCDEF", "x.py") == ScanVerdict.BLOCKED
        assert scanner.scan("warning: token here", "x.py") == ScanVerdict.FLAGGED

    def test_class_without_scan_does_not_satisfy_protocol(self) -> None:
        from mcp_server.application.ports.secret_scanner import SecretScannerPort

        class NotAScanner:
            pass

        assert not isinstance(NotAScanner(), SecretScannerPort)