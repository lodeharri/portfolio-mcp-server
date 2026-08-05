# ADR 001: Composition root style — eager vs lazy

- **Status**: Accepted
- **Date**: 2026-08-05
- **Change**: `001-bootstrap`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

`src/mcp_server/composition.py` is the single wiring point between concrete adapters (in `infrastructure/adapters/`) and use cases (in `application/use_cases/`). Two patterns are valid for a FastAPI app:

1. **Eager** — at `create_app()` time, instantiate every adapter, build every use case, freeze them into a `Container` that the FastAPI app closes over. First call to `/healthz` and first MCP tool call have zero wiring overhead.
2. **Lazy** — at request time, resolve the right adapter via `Depends(...)` / FastAPI's dependency injection graph. Adapters that are never called never get built. More bouncer-friendly semantic.

The deployment is a 256 MB Fly.io shared VM, single `--workers 1`, free-tier Gemini (no per-init billing), slowapi (stateful in-memory). Cold start is dominated by `import fastapi`, `import fastmcp`, and `import sqlite3`. The VectorStore adapter opens `data/index.sqlite`; the Embedding adapter builds a `genai.Client`.

## Decision Drivers

- **D1**: Fail-fast on misconfiguration (missing API key, missing manifest, missing DB) — surface at `uvicorn` startup, not at first user request.
- **D2**: Simplicity of test fixtures — tests want one `compose(test_config)` call returning a deterministic container.
- **D3**: Memory ceiling — 256 MB. One `genai.Client` is ~5 MB; trivial.
- **D4**: Hexagonal invariant — `composition.py` is the ONLY module that imports both adapters and use cases. Eager makes this trivially auditable via a single AST walk.
- **D5**: Spec scenario "Composition root is the only wiring point" must be testable.

## Considered Options

### Option A — Eager wiring (chosen)

`compose()` builds every adapter, every use case, returns a frozen `Container`. `create_app()` calls `compose()` once. `run()` is just uvicorn plumbing.

**Pros**:
- Fail-fast: missing GEMINI_API_KEY, missing manifest, unreadable DB all surface in `docker logs` before the first request.
- Single source of truth for which adapters are wired. The import-graph invariant becomes a one-file check.
- Test fixture = `container = compose(test_config)` — same call site as production.
- Cold-start cost: ~50 ms (genai client + sqlite open). Acceptable.

**Cons**:
- Even if only `/healthz` is ever hit, the embedding adapter's `genai.Client` is built. ~5 MB of RAM we don't need.
- If `data/index.sqlite` is missing and the user only wants `/healthz`, the VectorStore adapter needs a "no-DB" lazy fallback. Spec edge case covers this (`/healthz` MUST return 200 even with missing DB).

### Option B — Lazy wiring (rejected)

FastAPI `Depends(get_embedding_port)` etc. `compose()` returns a factory; each request resolves what it needs.

**Pros**:
- `/healthz` doesn't touch the Gemini client.
- Useful if adapters were expensive (they aren't).

**Cons**:
- First user query pays the wiring cost — recruiter demo experience is worse.
- Hexagonal invariant becomes harder to enforce (every `Depends` call is a wiring site).
- Spec test "Composition root is the only wiring point" cannot be a static check anymore — must rely on a convention.
- The VectorStore "no-DB fallback" question becomes a runtime branch in every call site instead of a startup decision.

### Option C — Hybrid (rejected)

Eager for security-critical adapters (scanner, manifest, sanitizer, audit, rate limiter); lazy for optional ones (Gemini client, LLM client). The line blurs immediately: when does something become "optional"? Better to pick one rule and stick to it.

## Decision

**Option A — Eager wiring.** `compose()` is called exactly once in `create_app()`. Adapters with hard dependencies on missing resources (DB file, API key) raise at startup, not at first request. The VectorStore has a `read_only_missing_ok=True` mode for the `/healthz` edge case so the app still boots when the index hasn't been built yet.

## Consequences

**Positive**:
- Predictable startup behavior — same logs every cold start.
- Trivial testability of the hexagonal invariant.
- The container is a `@dataclass(frozen=True)`, so once built it cannot be mutated accidentally mid-request.
- `create_app()` is idempotent for tests because each call constructs a new container.

**Negative**:
- Memory ceiling is closer to 100 MB from the start. Still well under 256 MB; measured in `verify` phase.
- First-call latency is constant (~few ms overhead from indirection through frozen dataclass). Acceptable.
- `data/index.sqlite` missing → app still boots but logs a WARN. Tools that need the index will return empty results. Spec edge case.

**Compliance with rules**:
- `rules.apply.guidelines` → "FastAPI composition-root pattern: app factory in src/mcp_server/app.py" — satisfied.
- `invariants` → "All MCP tool outputs pass through OutputSanitizer before reaching the client" — satisfied; sanitizer is a middleware, registered once at startup.

## Follow-ups

- In `001-bootstrap` apply phase: write a `tests/integration/test_hexagonal_invariants.py` that statically walks `src/mcp_server/` and asserts only `composition.py` imports both `infrastructure/adapters/` and `application/use_cases/`.
- In verify phase: measure memory at idle (`/healthz` only) and at load (10 concurrent MCP tool calls). Document in verify report.