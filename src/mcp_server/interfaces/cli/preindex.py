"""``preindex`` CLI — manifest-driven indexing pipeline entry point.

The CLI is the user-facing entry for:

* ``python -m mcp_server.interfaces.cli.preindex`` — dev invocation.
* ``preindex ...`` — ``[project.scripts]`` console_script after install.

Per ADR-002 the argparse surface is:

* ``--manifest PATH`` — overrides the configured manifest path.
* ``--db PATH`` — overrides the configured SQLite path.
* ``--mock-gemini`` — use the deterministic :class:`MockEmbeddingAdapter`.
* ``--no-mock-gemini-auto`` — disable the auto-fallback (require a key).
* ``--quiet`` — suppress per-file progress output.
* ``--limit-files N`` — cap files per project (dev convenience).

Auto-``--mock-gemini``: when ``GEMINI_API_KEY`` is unset AND
``--mock-gemini`` was not explicitly passed (and ``--no-mock-gemini-auto``
is not set), the CLI prints a WARN line and falls back to the mock
adapter so the builder always succeeds (spec scenario "Build without
GEMINI_API_KEY still succeeds").

The CLI exits with codes from :class:`mcp_server.domain.exceptions
.PreindexExitCode` so the Dockerfile ``RUN`` line can branch on them.

Hexagonal note
--------------

This module imports NOTHING from ``src/mcp_server/infrastructure/``.
The composition root (``src/mcp_server/composition.py``) is the only
module that crosses the application / infrastructure boundary; the
CLI consumes a fully-wired :class:`Composition` and uses the
exposed ``preindex_use_case``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from mcp_server.config import AppConfig, load_config
from mcp_server.domain.exceptions import (
    DomainError,
    GeminiTransientError,
    ManifestError,
    PreindexExitCode,
)

__all__ = ["build_parser", "cli", "main"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_gemini_key() -> bool:
    """True iff the AppConfig loaded a non-empty ``gemini_api_key``.

    The single-source-of-env rule means ONLY ``config.py`` reads
    ``os.environ``. The CLI reuses ``AppConfig.gemini_api_key`` (set
    from the env via ``load_config()``) as the single source of truth.
    """
    val = (load_config().gemini_api_key or "").strip()
    return bool(val)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser per ADR-002."""
    parser = argparse.ArgumentParser(
        prog="preindex",
        description=(
            "Manifest-driven indexing pipeline — embeds every chunk from the "
            "declared projects into data/index.sqlite. Re-runs are no-ops "
            "via the chunk-hash cache."
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to projects.manifest.yaml (default: from AppConfig).",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to the SQLite index file (default: data/index.sqlite).",
    )
    parser.add_argument(
        "--mock-gemini",
        action="store_true",
        default=None,
        help=(
            "Use the deterministic MockEmbeddingAdapter instead of the real "
            "Gemini SDK. If GEMINI_API_KEY is unset, falls back automatically "
            "unless --no-mock-gemini-auto is passed."
        ),
    )
    parser.add_argument(
        "--no-mock-gemini-auto",
        action="store_true",
        default=False,
        help="Disable the auto-mock-gemini fallback (requires GEMINI_API_KEY).",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=False,
        help="Suppress per-file progress output; keep audit on stderr.",
    )
    parser.add_argument(
        "--limit-files",
        type=int,
        default=None,
        help="Cap the number of files per project (dev convenience).",
    )
    return parser


def _resolve_mock_gemini(args: argparse.Namespace) -> bool:
    """Apply ADR-002 auto-fallback: ``--mock-gemini`` if no API key.

    Order of precedence:

    1. ``--mock-gemini`` explicit → True.
    2. ``--no-mock-gemini-auto`` (with no explicit flag) → False (require key).
    3. No API key → auto-fallback to mock with a WARN.
    4. Has key → False (use real adapter).
    """
    if args.mock_gemini:
        return True
    if args.no_mock_gemini_auto:
        return False
    if not _has_gemini_key():
        print(
            "WARN: GEMINI_API_KEY unset; falling back to --mock-gemini",
            file=sys.stderr,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """Parse args, build composition, run the preindex use case.

    Returns:
        An integer exit code matching :class:`PreindexExitCode`.
        ``0`` on success; non-zero on any documented failure.
    """
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    use_mock = _resolve_mock_gemini(args)

    config = load_config()
    if args.manifest is not None:
        config = config.model_copy(update={"manifest_path": args.manifest})

    # Build composition FIRST so manifest errors surface as MANIFEST_ERROR
    # (exit 2) before any DB I/O. The composition root owns the fail-fast
    # contract and the entire adapter/use-case wiring.
    try:
        from mcp_server.composition import create_composition

        # ``--db`` override — pass via config so composition opens the
        # right sqlite path (rather than the default ``data/index.sqlite``).
        if args.db is not None:
            config = config.model_copy(
                update={"data_dir": Path(args.db).parent}
            )
            config = config.model_copy(
                update={"_db_path_override": Path(args.db)}
            )
        comp = create_composition(config, use_mock_gemini=use_mock)
    except ManifestError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return PreindexExitCode.MANIFEST_ERROR.value
    except Exception as exc:  # noqa: BLE001 — DB open failures land here too
        # Distinguish a DB failure (exit 5) from anything else (exit 5 too,
        # since it's still a "preindex cannot run" condition).
        print(f"ERROR: failed to assemble composition: {exc}", file=sys.stderr)
        return PreindexExitCode.DB_ERROR.value

    if comp.preindex_use_case is None:
        print("ERROR: composition did not wire the preindex use case", file=sys.stderr)
        return PreindexExitCode.DB_ERROR.value

    projects = comp.manifest.projects()
    if not projects:
        print("ERROR: manifest declared no projects", file=sys.stderr)
        return PreindexExitCode.MANIFEST_ERROR.value

    overall = {
        "projects": 0,
        "files": 0,
        "chunks": 0,
        "cache_hits": 0,
        "blocked": 0,
        "flagged": 0,
        "errors": 0,
    }
    for project in projects:
        if not args.quiet:
            print(
                f"project.start id={project.id!r}",
                file=sys.stderr,
            )
        try:
            result = comp.preindex_use_case.execute(project.id)
        except GeminiTransientError as exc:
            print(f"ERROR: gemini embedding exhausted retries: {exc}", file=sys.stderr)
            return PreindexExitCode.GEMINI_ERROR.value
        except DomainError as exc:
            print(f"ERROR: pipeline failed: {exc}", file=sys.stderr)
            return PreindexExitCode.MANIFEST_ERROR.value
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: pipeline failed: {exc}", file=sys.stderr)
            return PreindexExitCode.DB_ERROR.value

        overall["projects"] += 1
        overall["files"] += result.processed
        overall["chunks"] += result.embedded
        overall["cache_hits"] += result.cache_hits
        overall["blocked"] += result.blocked
        overall["flagged"] += result.flagged
        overall["errors"] += len(result.errors)

    # Final summary on stdout (machine-readable JSON line).
    print(json.dumps(overall, sort_keys=True))
    return PreindexExitCode.OK.value


# ---------------------------------------------------------------------------
# cli — thin wrapper for the console_script
# ---------------------------------------------------------------------------


def cli(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point — returns the exit code."""
    return main(argv)
