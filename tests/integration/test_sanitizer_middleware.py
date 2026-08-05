"""Integration tests for the OutputSanitizer cross-cutting behaviour (Layer 3).

Layer 3 of the 5-layer security model is applied to every byte that
leaves the server. This file verifies the integration of the
:class:`OutputSanitizer` with the composition root: any secret-shaped
string passed through ``comp.sanitizer`` is redacted to
``[REDACTED]`` and recorded as an incident.

The full HTTP middleware path (rewriting the response body of a route)
is exercised in PR4 / 005-deploy when the OutputSanitizerMiddleware is
registered in ``create_app()``. This file focuses on the cross-cutting
adapter behaviour without the HTTP boundary.
"""

from __future__ import annotations

import pytest

from mcp_server.composition import create_composition
from mcp_server.config import AppConfig
from mcp_server.security.output_sanitizer import (
    OutputSanitizer,
    SanitizedOutput,
    SecretPattern,
)


class TestCompositionSanitizer:
    """``Composition.sanitizer`` is a real :class:`OutputSanitizer`."""

    def test_sanitizer_is_real(self) -> None:
        comp = create_composition(AppConfig())
        assert isinstance(comp.sanitizer, OutputSanitizer)

    def test_sanitizer_redacts_aws_key(self) -> None:
        comp = create_composition(AppConfig())
        result = comp.sanitizer.sanitize("AWS=AKIAIOSFODNN7EXAMPLE", source="test")
        assert "[REDACTED]" in result.redacted_text
        assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted_text
        assert len(result.incidents) >= 1
        assert result.incidents[0].pattern == SecretPattern.AWS

    def test_sanitizer_redacts_github_token(self) -> None:
        comp = create_composition(AppConfig())
        token = "ghp_" + "a" * 36
        result = comp.sanitizer.sanitize(f"GH={token}", source="test")
        assert token not in result.redacted_text
        assert result.incidents[0].pattern == SecretPattern.GITHUB


class TestCompositionSanitizerJson:
    """``sanitize_json`` recursively sanitizes dict values."""

    def test_dict_with_secrets_redacted(self) -> None:
        comp = create_composition(AppConfig())
        payload = {"key": "AKIAIOSFODNN7EXAMPLE", "safe": "value"}
        result = comp.sanitizer.sanitize_json(payload, source="tool-output")
        assert isinstance(result, SanitizedOutput)
        assert "AKIAIOSFODNN7EXAMPLE" not in result.redacted_text
        assert "[REDACTED]" in result.redacted_text


class TestCompositionSanitizerCleanText:
    """Clean text passes through unchanged."""

    def test_clean_text_no_incidents(self) -> None:
        comp = create_composition(AppConfig())
        result = comp.sanitizer.sanitize("def hello(): return 42", source="test")
        assert result.redacted_text == "def hello(): return 42"
        assert result.incidents == []


@pytest.mark.parametrize(
    "sample",
    [
        "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "ghp_" + "a" * 36,
        "sk-" + "b" * 48,
        "AIza" + "c" * 35,
        "api_key=secret123",
        "password: hunter2",
    ],
)
class TestCompositionSanitizerTableDriven:
    """Every documented :class:`SecretPattern` is redacted by the wired sanitizer."""

    def test_redacts(self, sample: str) -> None:
        comp = create_composition(AppConfig())
        result = comp.sanitizer.sanitize(sample, source="table-driven")
        assert "[REDACTED]" in result.redacted_text
        assert len(result.incidents) >= 1
