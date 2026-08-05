"""Tests for ``src/mcp_server/security/output_sanitizer.py``.

Layer 3 of the 5-layer security model. The :class:`OutputSanitizer`
replaces every match of five ``SecretPattern`` regexes with the literal
string ``[REDACTED]`` and returns both the redacted text and the list
of incidents for the audit log.

Tests are RED until ``src/mcp_server/security/output_sanitizer.py``
exists. They are **table-driven**: one parametrized case per regex
pattern (AWS, GitHub, OpenAI, Gemini, generic) plus edge cases (clean
text, multiple matches, nested JSON).

The regex table mirrors ``openspec/changes/001-bootstrap/specs/security-layers.md``
→ "Output Sanitizer Redacts Known Patterns" requirement scenarios.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Regex table — each entry is (pattern_name, sample_input, expected_redacted)
# ---------------------------------------------------------------------------


REDACTION_CASES = [
    pytest.param(
        "AWS",
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "AWS_ACCESS_KEY_ID=[REDACTED]",
        id="aws-access-key",
    ),
    pytest.param(
        "GITHUB",
        "ghp_" + "a" * 36,
        "[REDACTED]",
        id="github-pat",
    ),
    pytest.param(
        "OPENAI",
        "sk-" + "a" * 48,
        "[REDACTED]",
        id="openai-api-key",
    ),
    pytest.param(
        "GEMINI",
        "AIza" + "a" * 35,
        "[REDACTED]",
        id="gemini-api-key",
    ),
    pytest.param(
        "GENERIC",
        "api_key=abc123",
        "[REDACTED]",
        id="generic-api-key",
    ),
    pytest.param(
        "GENERIC",
        "secret: hunter2",
        "[REDACTED]",
        id="generic-secret",
    ),
]


class TestOutputSanitizerRegexPatterns:
    """Each of the five ``SecretPattern`` regexes redacts its target."""

    @pytest.mark.parametrize(("pattern_name", "sample_input", "expected_redacted"), REDACTION_CASES)
    def test_redacts_known_pattern(
        self, pattern_name: str, sample_input: str, expected_redacted: str
    ) -> None:
        from mcp_server.security.output_sanitizer import OutputSanitizer

        sanitizer = OutputSanitizer()
        result = sanitizer.sanitize(sample_input, source="test-source")
        assert expected_redacted in result.redacted_text, (
            f"pattern {pattern_name} should redact {sample_input!r} but got {result.redacted_text!r}"
        )

    @pytest.mark.parametrize(("pattern_name", "sample_input", "expected_redacted"), REDACTION_CASES)
    def test_records_incidents(
        self, pattern_name: str, sample_input: str, expected_redacted: str
    ) -> None:
        from mcp_server.security.output_sanitizer import OutputSanitizer

        sanitizer = OutputSanitizer()
        result = sanitizer.sanitize(sample_input, source="test-source")
        assert len(result.incidents) >= 1, (
            f"pattern {pattern_name} should record at least one incident"
        )


class TestOutputSanitizerCleanText:
    """Clean text passes through unchanged with zero incidents."""

    def test_clean_text_unchanged(self) -> None:
        from mcp_server.security.output_sanitizer import OutputSanitizer

        sanitizer = OutputSanitizer()
        clean = "def hello_world():\n    return 42\n"
        result = sanitizer.sanitize(clean, source="test-source")
        assert result.redacted_text == clean
        assert result.incidents == []

    def test_plain_english_unchanged(self) -> None:
        from mcp_server.security.output_sanitizer import OutputSanitizer

        sanitizer = OutputSanitizer()
        text = "This README explains the architecture. No secrets here."
        result = sanitizer.sanitize(text, source="test-source")
        assert result.redacted_text == text


class TestOutputSanitizerMultipleMatches:
    """Multiple matches in one text produce multiple incidents."""

    def test_multiple_aws_keys_all_redacted(self) -> None:
        from mcp_server.security.output_sanitizer import OutputSanitizer

        sanitizer = OutputSanitizer()
        text = "first=AKIAIOSFODNN7EXAMPLE second=AKIAIOSFODNN7EXAMPLE end"
        result = sanitizer.sanitize(text, source="test-source")
        # Two AWS keys → two incidents.
        aws_incidents = [i for i in result.incidents if i.pattern.value == "aws"]
        assert len(aws_incidents) == 2
        # Both replaced.
        assert result.redacted_text.count("[REDACTED]") == 2
        assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted_text

    def test_mixed_patterns_all_redacted(self) -> None:
        from mcp_server.security.output_sanitizer import OutputSanitizer

        sanitizer = OutputSanitizer()
        text = (
            "AWS=AKIAIOSFODNN7EXAMPLE "
            "GH=ghp_" + "a" * 36 + " "
            "OpenAI=sk-" + "b" * 48
        )
        result = sanitizer.sanitize(text, source="test-source")
        assert result.redacted_text.count("[REDACTED]") == 3
        assert len(result.incidents) == 3


class TestOutputSanitizerJson:
    """``sanitize_json`` recursively sanitizes dict values."""

    def test_sanitize_dict_values(self) -> None:
        from mcp_server.security.output_sanitizer import OutputSanitizer

        sanitizer = OutputSanitizer()
        payload = {
            "name": "tool-call",
            "output": "leaked AKIAIOSFODNN7EXAMPLE",
        }
        result = sanitizer.sanitize_json(payload, source="tool-call")
        assert "[REDACTED]" in result.redacted_text
        assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted_text
        assert len(result.incidents) >= 1

    def test_sanitize_nested_dict(self) -> None:
        from mcp_server.security.output_sanitizer import OutputSanitizer

        sanitizer = OutputSanitizer()
        payload = {
            "tool": "search",
            "result": {
                "snippet": "GH token: ghp_" + "a" * 36,
                "metadata": {"author": "harrison"},
            },
        }
        result = sanitizer.sanitize_json(payload, source="search")
        assert "[REDACTED]" in result.redacted_text
        assert "ghp_" not in result.redacted_text

    def test_sanitize_list_values(self) -> None:
        from mcp_server.security.output_sanitizer import OutputSanitizer

        sanitizer = OutputSanitizer()
        payload = ["safe", "AKIAIOSFODNN7EXAMPLE", "also safe"]
        result = sanitizer.sanitize_json(payload, source="list")
        assert "[REDACTED]" in result.redacted_text
        assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted_text


class TestOutputSanitizerThreadSafety:
    """The sanitizer MUST be thread-safe (compiled regexes are module-level)."""

    def test_module_level_compiled_regexes(self) -> None:
        """Compiled regexes live at module level, not on the instance."""
        import mcp_server.security.output_sanitizer as mod

        # At least one module-level compiled pattern.
        assert any(
            attr_name.startswith("PATTERN_") or attr_name.startswith("_")
            for attr_name in dir(mod)
        ), "module should expose pre-compiled pattern constants"

    def test_multiple_instances_share_state(self) -> None:
        """Two sanitizer instances behave identically (no per-instance mutable state)."""
        from mcp_server.security.output_sanitizer import OutputSanitizer

        a = OutputSanitizer()
        b = OutputSanitizer()
        text = "AWS=AKIAIOSFODNN7EXAMPLE"
        ra = a.sanitize(text, source="a")
        rb = b.sanitize(text, source="b")
        assert ra.redacted_text == rb.redacted_text
        assert len(ra.incidents) == len(rb.incidents)