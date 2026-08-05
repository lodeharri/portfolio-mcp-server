# ADR 002: Preindex CLI contract

- **Status**: Accepted
- **Date**: 2026-08-05
- **Change**: `001-bootstrap`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

The preindex pipeline (`src/mcp_server/interfaces/cli/preindex.py`) needs a stable CLI contract so:
- Developers can run it locally (`python -m mcp_server.interfaces.cli.preindex`).
- CI can run it (`pytest ... --mock-gemini` style — wait, that's a test, but CI also runs the real one in the builder stage).
- The Docker builder invokes it as `python -m mcp_server.interfaces.cli.preindex` (per `Dockerfile` line 60).
- The proposal + spec mention a `--mock-gemini` flag but the current `pyproject.toml` only exposes `mcp-server = "mcp_server.app:run"` — no console_script for preindex.

The argparse interface, the entry point declaration, and the `--mock-gemini` semantics all need to be locked down before `sdd-tasks` writes the implementation tasks.

## Decision Drivers

- **D1**: Idempotent — re-running is a no-op (already covered by chunk-hash cache).
- **D2**: Two run modes: real (uses `GEMINI_API_KEY`) and mock (deterministic, no network).
- **D3**: All flags overridable; manifest values are defaults.
- **D4**: Container build with no `GEMINI_API_KEY` must still produce a working image (spec scenario).
- **D5**: Exit codes map to `PreindexExitCode` enum so the Dockerfile can branch on them.

## Considered Options

### Option A — argparse with full flag surface (chosen)

```text
python -m mcp_server.interfaces.cli.preindex [--manifest PATH] [--db PATH]
                                              [--mock-gemini] [--quiet]
                                              [--chunk-size N] [--chunk-overlap N]
                                              [--limit-files N]
```

Plus a console_script in `pyproject.toml`:

```toml
[project.scripts]
mcp-server = "mcp_server.app:run"
preindex   = "mcp_server.interfaces.cli.preindex:main"
```

`main(argv: list[str] | None = None) -> int` returns the exit code. `python -m ...` and `preindex ...` are equivalent entry points.

### Option B — Click (rejected)

Click is a heavier dep and not in `pyproject.toml`. The CLI surface is small (5 flags). argparse is in the stdlib.

### Option C — Typer (rejected)

Same dep cost as Click. Adds value only for nested subcommands (we have none).

## Decision

**Option A — argparse with the flag surface above.**

### Flag semantics

| Flag | Default | Effect |
|---|---|---|
| `--manifest PATH` | from `AppConfig.manifest_path` (`config/projects.manifest.yaml`) | YAML to load |
| `--db PATH` | `data/index.sqlite` | SQLite output path |
| `--mock-gemini` | `False` (True if `GEMINI_API_KEY` unset) | Use deterministic hash-based mock; no outbound HTTP |
| `--quiet` | `False` | Suppress progress lines on stdout; keep audit on stderr |
| `--chunk-size N` | from `manifest.indexing.chunk_size` (1500) | Override manifest value |
| `--chunk-overlap N` | from `manifest.indexing.chunk_overlap` (200) | Override manifest value |
| `--limit-files N` | `0` (all) | Dev convenience; cap files per project |

**Auto-`--mock-gemini`**: if `GEMINI_API_KEY` is unset AND `--mock-gemini` was not explicitly passed, the CLI prints `WARN: GEMINI_API_KEY unset; falling back to --mock-gemini` and proceeds with the mock adapter. This satisfies the spec scenario "Build without GEMINI_API_KEY still succeeds".

**Exit codes** (from `PreindexExitCode`):

| Code | Constant | Meaning |
|---|---|---|
| 0 | `OK` | Success |
| 2 | `MANIFEST_ERROR` | Manifest missing / schema invalid / no projects declared |
| 3 | `GITLEAKS_ERROR` | gitleaks binary missing or crashed (fail-closed) |
| 4 | `GEMINI_ERROR` | API key invalid, all retries exhausted, 4xx other than 429 |
| 5 | `DB_ERROR` | SQLite I/O or schema failure |

### Output

- Progress: human-readable on stdout, line per project + line per 100 chunks. Suppressed by `--quiet`.
- Audit: structured JSON on stderr via `AuditLogger`. CI picks it up without parsing.
- Exit summary: `{"projects": N, "files": M, "chunks": K, "cache_hits": H, "blocked": B, "flagged": F, "elapsed_s": E}` on stdout, last line, JSON.

### `pyproject.toml` change

```toml
[project.scripts]
mcp-server = "mcp_server.app:run"
preindex   = "mcp_server.interfaces.cli.preindex:main"
```

## Consequences

**Positive**:
- Two equivalent ways to invoke (`python -m ...` and `preindex ...`) — neither breaks on different environments.
- Mock mode is automatic when the API key is missing, so the builder always produces a working image.
- Exit codes are machine-readable; the Dockerfile `RUN` line can choose to `|| echo "WARN"` on non-fatal codes.

**Negative**:
- argparse help text is less polished than Click/Typer. Acceptable for an internal CLI.
- `--limit-files` is a dev-only flag; it should not appear in `--help` after first release. Document as hidden flag in the help text.

**Compliance with rules**:
- Spec "Schema / Interface" section lists `main(argv: list[str] | None = None) -> int` — satisfied exactly.
- `rules.apply.guidelines` → "Manifest is read-only at runtime; mutating it requires a new SDD change" — CLI does not mutate the manifest, only reads it.
- Spec scenario "Build without GEMINI_API_KEY still succeeds" — satisfied via auto-fallback.

## Follow-ups

- Add a `preindex --dry-run` flag in a later change (not 001) to print what would be indexed without writing the DB.
- Document the exit codes in `README.md` under the "Operations" section.