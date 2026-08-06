# 005 / 006 / 007 — Follow-up Changes (Shadow SDD Audit)

## Why this file exists

Changes 005, 006, and 007 landed on `main` without going through the full
SDD lifecycle. There is no `openspec/changes/{name}/` directory, no formal
`spec.md` deltas, no `design.md`, no `tasks.md`, no `verify-report.md`,
no `archive-report.md`. They were implemented as follow-ups to
`002-mcp-tools` directly on the branch and merged to `main`.

This file is a **minimum audit trail** so future SDD phases have
visibility into what shipped and why. It is NOT a substitute for proper
spec deltas — those should be created for the next change that touches
the same areas (LangChain, search-code manifest, embeddings).

The `005/006/007` numbering is a reconstruction from session memory and
commit history. The change IDs were reserved but the corresponding
folders were never created.

## 005 — LangChain Integration (replaces Pydantic AI)

**Goal.** Replace custom sliding-window chunking + Pydantic AI agent with
LangChain + LangGraph ReAct agent. Centralize everything in a single
file for architectural cleanliness. Drop the bloat from
`google-generativeai` (which pulls `google-api-python-client`).

**Why a single file.** To prevent the project from ending up with two
agent frameworks coexisting. Locked decision — do not revisit without
explicit user consent.

**Key commits** (from `git log`):
- `5ed6dde feat(embeddings): add LangChain embedding adapter (single file)`
  (partial overlap with 007 — the embedding factory landed in the same
  commit chain)
- The LangChain wiring commit chain from 2026-08-05 (chunking + agent +
  gemini-sdk swap)

**Files changed:**
- `src/mcp_server/infrastructure/langchain.py` (NEW, single file) —
  `LangChainChunkingAdapter`, `LangChainAgentAdapter`,
  `LangChainEmbeddingAdapter` (added in 007), `_MockLangChainEmbeddingAdapter`,
  `_MockLangChainAgentAdapter`, `_MockAskPortfolioUseCase` for
  `--mock-gemini` mode
- `src/mcp_server/composition.py` — rewired to use the new factory for
  chunking + agent + embedding fields
- `pyproject.toml` — added `langchain`, `langgraph`,
  `pydantic-ai-slim[google]`; replaced `google-generativeai` with
  `google-genai`
- Container image impact: 676MB → 417MB (~250MB saved by dropping
  `google-api-python-client` bloat)

**Locked decisions** (do not revisit without explicit user consent):
- LangChain for everything (chunking + agent + embedding)
- LangGraph for the ReAct agent (NOT Pydantic AI)
- `google-genai` (new SDK, not deprecated `google-generativeai`)
- Single-file centralization in `infrastructure/langchain.py`

## 006 — Search-Code Manifest Refinement

**Goal.** Tighten the manifest's `include_extensions` to only index files
that demonstrate skills (source code + docs). Drop config files that
bloat the index with non-semantic content and add conservative
`exclude_paths` to cut volume.

**Key commit:**
- `f53bafc chore(manifest): strict include_extensions — only source code + docs`

**Files changed:**
- `config/projects.manifest.yaml`:
  - `include_extensions`: kept `.py .ts .tsx .js .jsx .md`; dropped
    `.yaml .yml .toml .json`
  - `exclude_paths`: expanded (tests, build, `.aws`, migrations, etc.) —
    ~70% volume reduction vs. unfiltered

## 007 — Embeddings via LangChain

**Goal.** Move the embedding adapter from the custom Gemini client
(`adapters/gemini_embedding.py`) to LangChain's unified interface,
keeping the single-file centralization principle established in 005.

**Key commit:**
- `5ed6dde feat(embeddings): add LangChain embedding adapter (single file)`
  (same commit chain as 005)

**Files changed:**
- `src/mcp_server/infrastructure/langchain.py` — added
  `LangChainEmbeddingAdapter`, `_MockLangChainEmbeddingAdapter`, and
  `create_langchain_embedding` factory
- `src/mcp_server/composition.py` — rewired the `embedding` field to
  use the new factory

**Open follow-up:** `src/mcp_server/infrastructure/adapters/gemini_embedding.py`
is still in the tree for some paths. It can be deprecated in a small
cleanup change once all embedding callsites route through the LangChain
adapter.

## What's NOT captured (known gaps)

- **No formal spec deltas.** The contracts live in code, not in
  `openspec/specs/`. The next change that touches LangChain, the
  manifest, or embeddings should produce proper spec deltas first.
- **No verify-report.** There is no formal record of test coverage
  deltas or spec-vs-impl drift analysis for 005/006/007. The current
  test suite (479 passed, 3 skipped) is the only evidence.
- **No ADRs.** The architectural decisions listed above are
  reconstructed from session memory and the code itself, not from
  formal decision records.

## Recommended follow-up

A single follow-up change (e.g., `008-langchain-formalize`) should:
1. Write proper spec deltas for the LangChunking + LangGraphAgent +
   LangChainEmbedding ports.
2. Add an ADR for the single-file centralization decision.
3. Deprecate `adapters/gemini_embedding.py` once all callsites are
   migrated.
4. Update `openspec/specs/mcp-tools/spec.md` to reflect the current
   LangChain-backed behavior.