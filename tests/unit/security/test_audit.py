"""Tests for ``src/mcp_server/security/audit.py``.

Layer 5 of the 5-layer security model. The :class:`AuditLogger`
emits structlog JSON to stdout with at minimum the fields ``event``,
``timestamp``, ``level``, plus free-form ``**fields`` per call.

Tests are RED until the audit module exists. They capture stdout via
``capsys``, parse the emitted JSON line, and assert the documented
shape (event + timestamp + level + caller fields).

Threat-matrix coverage
----------------------

* "Secret log leak via audit JSON" — when the audit logger is called
  with a ``source=`` that contains a token-shaped string, the
  ``source`` field MUST appear as ``[REDACTED]`` in the JSON output.
  This is the OutputSanitizer wired into the audit pipeline.
"""

from __future__ import annotations

import json

import pytest


def _read_json_lines(captured: str) -> list[dict]:
    """Parse newline-delimited JSON from a ``capsys`` capture."""
    lines = [line for line in captured.splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


# ---------------------------------------------------------------------------
# Audit event shape
# ---------------------------------------------------------------------------


class TestAuditLoggerEmitsJson:
    """``audit.warn(event, **fields)`` emits a single JSON line."""

    def test_warn_emits_single_json_line(self, capsys: pytest.CaptureFixture[str]) -> None:
        from mcp_server.security.audit import AuditLogger

        audit = AuditLogger()
        audit.warn("secret.blocked", source="x.py", pattern="AWS")

        out, _ = capsys.readouterr()
        records = _read_json_lines(out)
        assert len(records) == 1

    def test_record_has_required_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        from mcp_server.security.audit import AuditLogger

        audit = AuditLogger()
        audit.warn("secret.blocked", source="x.py", pattern="AWS")

        out, _ = capsys.readouterr()
        record = _read_json_lines(out)[0]

        # Required fields per the security-layers.md spec.
        assert record["event"] == "secret.blocked"
        assert record["source"] == "x.py"
        assert record["pattern"] == "AWS"
        assert "timestamp" in record
        # ISO-8601 timestamps contain 'T' and end with '+00:00' or 'Z'.
        assert "T" in record["timestamp"]
        # Level field is part of the documented shape.
        assert "level" in record

    def test_info_level_is_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        from mcp_server.security.audit import AuditLogger

        audit = AuditLogger()
        audit.info("tool.invoked", tool="search")

        out, _ = capsys.readouterr()
        record = _read_json_lines(out)[0]
        assert record["level"] == "info"
        assert record["event"] == "tool.invoked"
        assert record["tool"] == "search"

    def test_warn_level_is_warn(self, capsys: pytest.CaptureFixture[str]) -> None:
        from mcp_server.security.audit import AuditLogger

        audit = AuditLogger()
        audit.warn("rate_limit.exceeded", client_ip="1.2.3.4")

        out, _ = capsys.readouterr()
        record = _read_json_lines(out)[0]
        assert record["level"] == "warning"
        assert record["event"] == "rate_limit.exceeded"
        assert record["client_ip"] == "1.2.3.4"


# ---------------------------------------------------------------------------
# Supported event types — per orchestrator's PR2 spec
# ---------------------------------------------------------------------------


class TestAuditLoggerEventTypes:
    """The five documented event types all emit valid JSON."""

    @pytest.mark.parametrize(
        "call_args",
        [
            pytest.param(
                ("secret.blocked", {"source": "x.py", "pattern": "AWS"}),
                id="secret.blocked",
            ),
            pytest.param(
                ("secret.flagged", {"source": "y.py", "pattern": "GEMINI"}),
                id="secret.flagged",
            ),
            pytest.param(
                ("rate_limit.exceeded", {"client_ip": "1.2.3.4"}),
                id="rate_limit.exceeded",
            ),
            pytest.param(
                ("tool.invoked", {"tool": "search", "query": "auth"}),
                id="tool.invoked",
            ),
            pytest.param(
                ("output.redacted", {"route": "/healthz", "count": 1}),
                id="output.redacted",
            ),
        ],
    )
    def test_supported_event_types_emit_valid_json(
        self,
        capsys: pytest.CaptureFixture[str],
        call_args: tuple[str, dict],
    ) -> None:
        from mcp_server.security.audit import AuditLogger

        event, fields = call_args
        audit = AuditLogger()
        audit.warn(event, **fields)

        out, _ = capsys.readouterr()
        record = _read_json_lines(out)[0]
        assert record["event"] == event
        # Every key in fields appears verbatim in the JSON record.
        for key, value in fields.items():
            assert record[key] == value


# ---------------------------------------------------------------------------
# Threat-matrix: secret log leak via audit JSON
# ---------------------------------------------------------------------------


class TestAuditLoggerSanitizesSourceField:
    """``source=`` containing a token MUST have the token replaced in JSON.

    The threat-matrix mitigation is to run the ``source`` field through
    :class:`OutputSanitizer`. The sanitizer replaces the matched token
    substring with ``[REDACTED]``; the surrounding path (without the
    token) is preserved so the audit log stays useful for forensics.
    """

    def test_source_with_aws_key_is_redacted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mcp_server.security.audit import AuditLogger

        audit = AuditLogger()
        # Token-shaped path leaks through if the audit logger doesn't
        # sanitize. The OutputSanitizer is wired into the audit pipeline
        # so the JSON MUST NOT contain the raw AWS key.
        malicious_source = "/home/user/keys/AKIAIOSFODNN7EXAMPLE"
        audit.warn("secret.blocked", source=malicious_source, pattern="AWS")

        out, _ = capsys.readouterr()
        record = _read_json_lines(out)[0]

        # The token is replaced with [REDACTED]; the surrounding path
        # is preserved so forensic context stays.
        assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(record), (
            "audit JSON leaked the AWS access key"
        )
        assert "[REDACTED]" in record["source"]

    def test_source_with_github_token_is_redacted(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mcp_server.security.audit import AuditLogger

        audit = AuditLogger()
        github_token = "ghp_" + "a" * 36
        malicious_source = f"/home/user/{github_token}"
        audit.warn("secret.blocked", source=malicious_source, pattern="GITHUB")

        out, _ = capsys.readouterr()
        record = _read_json_lines(out)[0]

        assert github_token not in json.dumps(record), (
            "audit JSON leaked the GitHub token"
        )
        assert "[REDACTED]" in record["source"]


# ---------------------------------------------------------------------------
# Constructor behaviour
# ---------------------------------------------------------------------------


class TestAuditLoggerConstruction:
    """``AuditLogger()`` constructs without raising and writes to stdout."""

    def test_default_construction_succeeds(self) -> None:
        from mcp_server.security.audit import AuditLogger

        audit = AuditLogger()
        assert audit is not None

    def test_two_instances_share_event_format(self, capsys: pytest.CaptureFixture[str]) -> None:
        from mcp_server.security.audit import AuditLogger

        a = AuditLogger()
        b = AuditLogger()
        a.warn("x", foo=1)
        b.warn("x", foo=2)

        out, _ = capsys.readouterr()
        records = _read_json_lines(out)
        assert len(records) == 2
        # Both events have the same envelope (event, level, timestamp).
        assert records[0]["event"] == "x"
        assert records[1]["event"] == "x"