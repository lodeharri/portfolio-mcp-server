"""Hexagonal architecture invariant tests.

Walks ``src/mcp_server/`` with ``ast`` and asserts the dependency rules from
``openspec/config.yaml`` and the spec for change ``001-bootstrap``:

- ``domain/`` is pure — no imports from ``application/``, ``infrastructure/``,
  ``interfaces/``, or ``security/``.
- ``application/use_cases/`` does not depend on ``infrastructure/`` or
  ``interfaces/`` — only on ``domain/`` + ``application/ports/``.
- ``interfaces/`` does not depend on ``infrastructure/`` — only on
  ``application/use_cases/`` + ``domain/``.
- ``src/mcp_server/composition.py`` is the ONLY module that wires concrete
  adapters (``infrastructure/adapters/``) to use cases
  (``application/use_cases/``) — see
  ``openspec/changes/001-bootstrap/design/adrs/001-composition-eager-vs-lazy.md``.

These invariants are the hexagonal backbone. If any of these assertions fail,
the architectural contract is broken — do not "fix" the test; fix the
dependency direction in the offending module.

The tests are RED until the structure is wired correctly: ``composition.py``
must exist as the single wiring point, and no other module may import across
the forbidden boundaries.
"""

from __future__ import annotations

import ast
import pathlib
from typing import NamedTuple

import pytest

SRC_ROOT = pathlib.Path("src/mcp_server")
ADAPTERS_PREFIX = "mcp_server.infrastructure.adapters"
USE_CASES_PREFIX = "mcp_server.application.use_cases"
COMPOSITION_PATH = SRC_ROOT / "composition.py"


class ForbiddenImport(NamedTuple):
    """A single illegal import discovered by the invariant walker."""

    file: str  # path relative to repo root
    line: int
    forbidden_root: str  # e.g. "mcp_server.application"
    module: str  # the imported module that triggered the violation


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _iter_python_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Return all ``*.py`` files under ``root``, sorted for deterministic order."""
    return sorted(p for p in root.rglob("*.py"))


def _imported_module_names(tree: ast.AST) -> set[str]:
    """Collect the set of top-level module names referenced by any import.

    Handles both ``import a.b.c`` and ``from a.b import c``. Returns the
    full dotted module path (e.g. ``"mcp_server.application.use_cases.x"``).
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _is_under_prefix(module: str, prefix: str) -> bool:
    """True iff ``module`` equals ``prefix`` or starts with ``prefix + "."``."""
    return module == prefix or module.startswith(prefix + ".")


def _rel_to_repo(file_path: pathlib.Path) -> str:
    """Path relative to the repo root, using forward slashes."""
    return file_path.resolve().relative_to(pathlib.Path.cwd().resolve()).as_posix()


# ---------------------------------------------------------------------------
# Rule predicates
# ---------------------------------------------------------------------------


def _domain_violations(file_path: pathlib.Path, modules: set[str]) -> list[ForbiddenImport]:
    """Forbidden: anything in domain/ that imports application/infrastructure/interfaces/security."""
    rel = file_path.relative_to(SRC_ROOT).as_posix()
    if not (rel.startswith("domain/") and not rel.endswith("/__init__.py")):
        return []
    violations: list[ForbiddenImport] = []
    tree = ast.parse(file_path.read_text(), filename=str(file_path))
    line_map = _build_line_map(tree)
    for forbidden in (
        "mcp_server.application",
        "mcp_server.infrastructure",
        "mcp_server.interfaces",
        "mcp_server.security",
    ):
        for module in sorted(modules):
            if _is_under_prefix(module, forbidden):
                violations.append(
                    ForbiddenImport(
                        file=_rel_to_repo(file_path),
                        line=line_map.get(module, 0),
                        forbidden_root=forbidden,
                        module=module,
                    )
                )
    return violations


def _use_case_violations(file_path: pathlib.Path, modules: set[str]) -> list[ForbiddenImport]:
    """Forbidden: anything in application/use_cases/ that imports infrastructure or interfaces."""
    rel = file_path.relative_to(SRC_ROOT).as_posix()
    if not (rel.startswith("application/use_cases/") and not rel.endswith("/__init__.py")):
        return []
    violations: list[ForbiddenImport] = []
    tree = ast.parse(file_path.read_text(), filename=str(file_path))
    line_map = _build_line_map(tree)
    for forbidden in ("mcp_server.infrastructure", "mcp_server.interfaces"):
        for module in sorted(modules):
            if _is_under_prefix(module, forbidden):
                violations.append(
                    ForbiddenImport(
                        file=_rel_to_repo(file_path),
                        line=line_map.get(module, 0),
                        forbidden_root=forbidden,
                        module=module,
                    )
                )
    return violations


def _interfaces_violations(file_path: pathlib.Path, modules: set[str]) -> list[ForbiddenImport]:
    """Forbidden: anything in interfaces/ that imports infrastructure."""
    rel = file_path.relative_to(SRC_ROOT).as_posix()
    if not (rel.startswith("interfaces/") and not rel.endswith("/__init__.py")):
        return []
    violations: list[ForbiddenImport] = []
    tree = ast.parse(file_path.read_text(), filename=str(file_path))
    line_map = _build_line_map(tree)
    for forbidden in ("mcp_server.infrastructure",):
        for module in sorted(modules):
            if _is_under_prefix(module, forbidden):
                violations.append(
                    ForbiddenImport(
                        file=_rel_to_repo(file_path),
                        line=line_map.get(module, 0),
                        forbidden_root=forbidden,
                        module=module,
                    )
                )
    return violations


def _build_line_map(tree: ast.AST) -> dict[str, int]:
    """Map imported module name → first line number it appears on.

    Used for human-readable violation messages.
    """
    line_map: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                line_map.setdefault(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module:
            line_map.setdefault(node.module, node.lineno)
    return line_map


# ---------------------------------------------------------------------------
# Parametrized rule check
# ---------------------------------------------------------------------------


def _format_violations(violations: list[ForbiddenImport]) -> str:
    parts = [
        f"  {v.file}:{v.line}  imports '{v.module}' (forbidden root '{v.forbidden_root}')"
        for v in violations
    ]
    return "\n".join(parts)


def test_domain_is_pure() -> None:
    """``domain/`` MUST NOT import from application/infrastructure/interfaces/security."""
    files = _iter_python_files(SRC_ROOT)
    all_violations: list[ForbiddenImport] = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        modules = _imported_module_names(tree)
        all_violations.extend(_domain_violations(path, modules))
    assert not all_violations, (
        "Hexagonal invariant violated — domain/ must be pure:\n"
        + _format_violations(all_violations)
    )


def test_application_use_cases_do_not_import_infrastructure_or_interfaces() -> None:
    """``application/use_cases/`` MUST NOT import infrastructure or interfaces."""
    files = _iter_python_files(SRC_ROOT)
    all_violations: list[ForbiddenImport] = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        modules = _imported_module_names(tree)
        all_violations.extend(_use_case_violations(path, modules))
    assert not all_violations, (
        "Hexagonal invariant violated — application/use_cases/ must only depend on "
        "domain/ and application/ports/:\n" + _format_violations(all_violations)
    )


def test_interfaces_do_not_import_infrastructure() -> None:
    """``interfaces/`` MUST NOT import infrastructure — only application + domain."""
    files = _iter_python_files(SRC_ROOT)
    all_violations: list[ForbiddenImport] = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        modules = _imported_module_names(tree)
        all_violations.extend(_interfaces_violations(path, modules))
    assert not all_violations, (
        "Hexagonal invariant violated — interfaces/ must not import infrastructure:\n"
        + _format_violations(all_violations)
    )


def test_composition_root_exists() -> None:
    """``src/mcp_server/composition.py`` MUST exist as the single wiring point (ADR-001)."""
    assert COMPOSITION_PATH.exists(), (
        "src/mcp_server/composition.py must exist as the composition root "
        "(see openspec/changes/001-bootstrap/design/adrs/001-composition-eager-vs-lazy.md)"
    )


def test_composition_root_is_only_wiring_point() -> None:
    """``composition.py`` MUST be the ONLY module importing both adapters and use cases.

    Other modules may import from application/use_cases (interfaces do), and
    other modules may import from infrastructure/adapters (none should, but if
    any do, they MUST NOT also import use_cases — that is composition.py's
    exclusive job).

    Composition MUST do both, otherwise the wiring invariant is not satisfied.
    """
    files = _iter_python_files(SRC_ROOT)
    both_importers: list[pathlib.Path] = []
    for path in files:
        tree = ast.parse(path.read_text(), filename=str(path))
        modules = _imported_module_names(tree)
        imports_adapters = any(_is_under_prefix(m, ADAPTERS_PREFIX) for m in modules)
        imports_use_cases = any(_is_under_prefix(m, USE_CASES_PREFIX) for m in modules)
        if imports_adapters and imports_use_cases:
            both_importers.append(path)

    non_composition = [p for p in both_importers if p.resolve() != COMPOSITION_PATH.resolve()]
    assert not non_composition, (
        "Hexagonal invariant violated — only composition.py may wire adapters to use cases. "
        "Offending modules:\n"
        + "\n".join(f"  {_rel_to_repo(p)}" for p in non_composition)
    )

    # And composition.py must do both — otherwise the invariant is unsatisfiable.
    assert COMPOSITION_PATH.exists(), (
        "composition.py must exist; cannot enforce wiring-point invariant without it"
    )
    tree = ast.parse(COMPOSITION_PATH.read_text(), filename=str(COMPOSITION_PATH))
    modules = _imported_module_names(tree)
    imports_adapters = any(_is_under_prefix(m, ADAPTERS_PREFIX) for m in modules)
    imports_use_cases = any(_is_under_prefix(m, USE_CASES_PREFIX) for m in modules)
    assert imports_adapters, (
        f"composition.py must import from {ADAPTERS_PREFIX} (it wires concrete adapters per ADR-001)"
    )
    assert imports_use_cases, (
        f"composition.py must import from {USE_CASES_PREFIX} (it wires use cases per ADR-001)"
    )


# ---------------------------------------------------------------------------
# Extra invariants the orchestrator prompt pinned to this PR
# ---------------------------------------------------------------------------


def test_only_config_module_reads_os_environ() -> None:
    """``src/mcp_server/config.py`` MUST be the ONLY module importing ``os.environ``.

    The single-source-of-env invariant from the orchestrator's PR1 spec — any
    other module that needs configuration MUST go through ``AppConfig`` /
    ``load_config()``.
    """
    files = _iter_python_files(SRC_ROOT)
    offenders: list[tuple[str, int, str]] = []  # (file, line, snippet)
    for path in files:
        if path.resolve() == (SRC_ROOT / "config.py").resolve():
            continue
        text = path.read_text()
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                # Match `os.environ`, `os.environ.get`, `os.environ[]`
                if (
                    isinstance(node.value, ast.Name)
                    and node.value.id == "os"
                    and node.attr == "environ"
                ):
                    snippet = ast.unparse(node).strip()
                    offenders.append((_rel_to_repo(path), node.lineno, snippet))
    assert not offenders, (
        "Hexagonal invariant violated — only src/mcp_server/config.py may read os.environ. "
        "Pass configuration through AppConfig instead.\n"
        + "\n".join(f"  {f}:{ln}  {snip}" for f, ln, snip in offenders)
    )


@pytest.fixture(scope="module")
def src_root() -> pathlib.Path:
    """Convenience fixture: the src/mcp_server/ root, absolute and resolved."""
    return SRC_ROOT.resolve()
