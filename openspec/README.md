# OpenSpec — portfolio-mcp-server

This directory is the source of truth for spec-driven development on the `portfolio-mcp-server` project.

## Layout

```
openspec/
├── config.yaml              <- SDD session preflight, testing capabilities, invariants
├── specs/                   <- Main specs (source of truth). Created per-domain in changes.
└── changes/                 <- Active changes (one folder per change).
    └── archive/             <- Completed changes (YYYY-MM-DD-{name}/).
```

## Current state (2026-08-04, init phase)

- `config.yaml` populated with session preflight, stack, testing capabilities, and cross-phase invariants.
- `specs/` and `changes/` are empty. The first change `001-bootstrap` will populate both.

## How to read this in a future session

1. Start with `openspec/config.yaml` — it carries pace, artifact store, review budget, and the cross-phase invariants.
2. List `openspec/changes/` to see active work.
3. For each active change, the orchestrator reads `state.yaml` (once created) to recover phase progress.
4. After `sdd-archive`, completed changes live under `openspec/changes/archive/YYYY-MM-DD-{name}/` and the main `specs/` is updated.

## How a change progresses

```
proposal → spec → design → tasks → apply → verify → archive
```

Each phase writes one or more artifacts into `openspec/changes/{name}/`. The orchestrator owns `state.yaml`; the phase skills own their respective files.
