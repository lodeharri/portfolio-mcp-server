"""Regression tests for ``pyproject.toml`` dependency pins.

The 003-playground-ui / PR2a spec mandates
``langgraph>=0.2,<2.0`` so the LangGraph ``stream_mode="messages"``
event surface does not drift across major releases. A test on the
file keeps the pin from being loosened by a careless refactor.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _read_langgraph_pin() -> str | None:
    """Return the literal string declared for ``langgraph`` in pyproject.

    Scans the ``[project] dependencies`` block for an entry whose name
    starts with ``"langgraph"`` (case-sensitive — we match the exact
    key). Returns ``None`` when the dependency is absent.
    """
    text = PYPROJECT_PATH.read_text()
    # Match `"langgraph..."` inside the dependencies list. The pin is
    # a quoted string optionally followed by a `# comment` so we use a
    # non-greedy regex up to the closing quote or ``#``.
    match = re.search(r'"(langgraph[^"]*)"', text)
    return match.group(1) if match else None


def test_langgraph_pin_matches_spec() -> None:
    """`langgraph` must be pinned to ``>=0.2,<2.0`` per the agent-streaming spec."""
    pin = _read_langgraph_pin()
    assert pin is not None, "langgraph must be declared in pyproject.toml"
    assert pin.startswith("langgraph>="), (
        f"langgraph pin must use a lower bound specifier, got: {pin!r}"
    )
    # Pull the operator strings. Order matters — must appear in order.
    # Spec: ``langgraph>=0.2,<2.0`` — two specifier parts separated by a comma.
    assert ",<2.0" in pin, f"langgraph pin must include an upper bound '<2.0', got: {pin!r}"
    assert re.search(r">=0\.2(\.0)?", pin), (
        f"langgraph pin must include '>=0.2' lower bound, got: {pin!r}"
    )


@pytest.mark.parametrize(
    ("installed_version", "expected_ok"),
    [
        ("0.2.0", True),
        ("0.3.5", True),
        ("1.0.0", True),
        ("1.2.10", True),
        ("1.99.99", True),
        ("2.0.0", False),
        ("2.1.0", False),
        ("0.1.99", False),
    ],
)
def test_langgraph_pin_specifier_accepts_versions(
    installed_version: str, expected_ok: bool
) -> None:
    """Spec semantics: ``>=0.2,<2.0`` accepts [0.2, 2.0) — guard against typos.

    Pure logic test — no pip installation required.
    """
    pin = _read_langgraph_pin()
    assert pin is not None
    # Strip the package name and validate via packaging.specifiers.
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    spec_set = SpecifierSet(pin.removeprefix("langgraph"))
    ok = Version(installed_version) in spec_set
    assert ok is expected_ok, (
        f"version {installed_version} should {'pass' if expected_ok else 'fail'} the pin {pin!r}"
    )
