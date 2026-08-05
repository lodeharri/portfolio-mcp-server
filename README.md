# mcp-server-playground

Harrison Rodriguez's second portfolio project: an **MCP (Model Context Protocol) server in Python** with a built-in **web playground** for a recruiter-facing demo.

Sibling to [`finance-coach-latam`](../finance-coach-latam) and [`landing-page-portfolio`](../landing-page-portfolio) inside the parent [`portfolio/`](../) folder.

> **Status:** greenfield. This README is the pre-`change 001-bootstrap` scaffold. Real content lands once the bootstrap change ships.

## Why this project exists

The current portfolio is heavy on architecture and light on live demos. This piece fills the gaps:

- **Python backend** showcase (the rest is TypeScript).
- **Container-based deploy** (Fly.io, not serverless like `finance-coach-latam`).
- **AI infrastructure** (MCP is the protocol layer; Gemini powers the tools).
- **Live interactive demo** for recruiters (the playground tab).

## Stack at a glance

| Layer        | Choice                                                |
| ------------ | ----------------------------------------------------- |
| Backend      | FastAPI + FastMCP (Python MCP SDK), single process    |
| Frontend     | HTMX + htmx-ws + Jinja2 templates (no separate Node)  |
| Database     | SQLite + `sqlite-vec` (vector search, no Neon needed) |
| LLM          | Gemini 2.0 Flash + text-embedding-004 (free tier)     |
| Agent        | Pydantic AI (playground chat only, for tool calling)  |
| Jobs         | arq (Redis-backed, optional)                          |
| Tests        | pytest + pytest-asyncio + httpx.AsyncClient + Playwright + axe-core |
| Lint/format  | ruff (Python) + prettier (Jinja/HTML/JS)              |
| Container    | Docker multi-stage, target <150 MB                    |
| Deploy       | Fly.io free tier (256 MB shared VMs), $0/month target |

## 5-layer security model (mandatory, all free)

1. **Manifest scoping** — `config/projects.manifest.yaml` declares which projects to index. Default-deny.
2. **Pre-index secret scan** — `gitleaks-python` runs on every chunk. High-confidence → block, medium → flag.
3. **Runtime output sanitization** — regex filter for AWS / GitHub / OpenAI / Gemini / generic credentials. Redacts to `[REDACTED]` and logs the incident.
4. **Pre-commit + CI secret scan** — `gitleaks` pre-commit hook + CI `gitleaks detect --redact` + GitHub secret scanning ON.
5. **Rate limit + audit log** — `slowapi` at ~30 req/min/IP. Every query logged with timestamp, IP, query, count, redactions. Alert on anomalous redaction rates.

## MCP tool catalog (initial)

| Tool                          | Purpose                                       |
| ----------------------------- | --------------------------------------------- |
| `list_portfolio_projects()`   | List projects from the manifest                |
| `search_harrison_code(query, language?)` | Semantic code search over the index   |
| `explain_architecture(project)`          | Architecture + ADRs (uses Gemini)    |
| `summarize_readme(repo)`                  | Gemini summary of a project README   |
| `get_architecture_diagram(project)`       | SVG architecture diagram, base64     |
| `ask_portfolio(question)`                 | Pydantic AI agent with tool calling (streaming in playground) |

## Folder layout

Top-level (separation between source code, frontend, infra, and SDD):

```
mcp-server-playground/
├── README.md                        <- you are here
├── pyproject.toml
├── Dockerfile                       <- multi-stage, <150 MB target
├── fly.toml                         <- Fly.io deploy config
├── .pre-commit-config.yaml          <- gitleaks + ruff + prettier
├── .dockerignore
├── .github/workflows/               <- test, lint, secret-scan, deploy
│
├── config/                          <- top-level configuration
│   └── projects.manifest.yaml       <- single source of truth for indexing
│
├── src/                             <- BACKEND (Python src layout)
│   └── mcp_server/                  <- hexagonal architecture — see below
│
├── playground/                      <- FRONTEND (HTML + assets)
│   ├── templates/                   <- Jinja templates
│   └── static/                      <- htmx.min.js, htmx-ws.js, styles.css
│
├── tests/                           <- Tests mirror src/ structure
│   ├── unit/{domain,application,infrastructure,security}/
│   ├── integration/
│   └── e2e/playground/
│
├── data/                            <- Generated artifacts (gitignored)
│   └── index.sqlite
│
└── openspec/                        <- SDD artifacts
    ├── config.yaml
    ├── changes/
    └── specs/
```

`src/mcp_server/` follows **hexagonal architecture** (Ports & Adapters). Dependencies point INWARD toward the pure domain:

```
src/mcp_server/
├── app.py                           <- FastAPI app factory
├── config.py                        <- Typed config (env vars only)
├── composition.py                   <- DI container: wires adapters → use cases
│
├── domain/                          <- PURE (no framework deps)
│   ├── entities.py                  <- CodeChunk, Project, SearchResult
│   ├── value_objects.py             <- ChunkHash, Vector, Embedding
│   └── exceptions.py
│
├── application/                     <- USE CASES + PORTS
│   ├── ports/                       <- Abstract interfaces (Protocols)
│   │   ├── embedding.py             <- EmbeddingPort
│   │   ├── vector_store.py          <- VectorStorePort
│   │   ├── llm.py                   <- LLMPort
│   │   ├── secret_scanner.py        <- SecretScannerPort
│   │   ├── manifest.py              <- ManifestPort
│   │   └── rate_limiter.py          <- RateLimiterPort
│   └── use_cases/
│       ├── index_project.py
│       ├── search_code.py
│       ├── explain_architecture.py
│       ├── summarize_readme.py
│       ├── list_projects.py
│       └── ask_portfolio.py
│
├── infrastructure/                  <- ADAPTERS (concrete implementations)
│   ├── adapters/
│   │   ├── gemini_embedding.py      <- implements EmbeddingPort
│   │   ├── sqlite_vec_store.py      <- implements VectorStorePort
│   │   ├── gemini_llm.py            <- implements LLMPort
│   │   ├── gitleaks_scanner.py      <- implements SecretScannerPort
│   │   ├── yaml_manifest.py         <- implements ManifestPort
│   │   └── slowapi_rate_limiter.py  <- implements RateLimiterPort
│   └── db/
│       ├── schema.sql
│       └── connection.py
│
├── interfaces/                      <- THE OUTSIDE WORLD
│   ├── mcp/                         <- FastMCP server + tools
│   ├── http/                        <- FastAPI routes (/healthz, etc.)
│   └── cli/                         <- preindex.py CLI entry
│
└── security/                        <- Cross-cutting concerns
    ├── output_sanitizer.py          <- Layer 3 regex sanitizer
    └── audit.py                     <- Layer 5 structured logging
```

**SOLID + hexagonal rules:**
- `domain/` has **zero** imports from `application/`, `infrastructure/`, or `interfaces/`.
- `application/use_cases/` depends only on `domain/` + `application/ports/`. Never on concrete adapters.
- `infrastructure/adapters/` implements `application/ports/`. One adapter per port.
- `interfaces/` depends on `application/use_cases/`. Never on `infrastructure/` directly — only through `composition.py`.
- `composition.py` is the **only** module that wires concrete adapters to use cases.

## Free-tier limits (document for honesty)

- **Primary deploy (Fly.io)**: ~$2.02/mo for 256 MB shared CPU 24/7. Fly.io free tier was discontinued Oct 2024 for new users — $0/mo target no longer realistic. Deploy is platform-agnostic: same Dockerfile works on Hugging Face Spaces ($0, cold starts), Render ($0, sleep), Railway ($5 credit).
- **Gemini free tier**: 15 RPM / 1M TPM for Flash, 1500 RPM for `text-embedding-004`. Sufficient for recruiter demo traffic. Preindex.py has checkpoint/resume + chunk-hash caching to handle RPD limits gracefully.
- **GitHub Actions**: 2000 min/month free. We gate deploys behind a cheap pre-flight test.
- **Custom domain `mcp.lodeharri.dev`**: optional, ~$10-15/year if desired.

## Project conventions

- **Spec-driven development.** All changes go through `openspec/changes/{name}/` (proposal → spec → design → tasks → apply → verify → archive).
- **Strict TDD.** RED → GREEN → REFACTOR per task. Coverage gate: 60% on `src/mcp_server/` unit tests.
- **Conventional commits.** No "Co-Authored-By" lines. No AI attribution in commits.
- **Hexagonal architecture (Ports & Adapters) + SOLID.** Domain is pure Python (no framework deps). Use cases depend on Ports (Protocols). Adapters implement Ports. `composition.py` is the only wiring point. Dependency direction: `interfaces → application → domain`, never reversed. `infrastructure → application` (implements ports).

## Next step

The next recommended phase is `sdd-propose` for **`change 001-bootstrap`**: install the declared deps, scaffold `src/mcp_server/app.py` with `/healthz` + port-agnostic binding, wire the 5 security layers in code (manifest loader, gitleaks wrapper, output sanitizer, slowapi limiter, audit log), implement `scripts/preindex.py` with chunk-hash caching + checkpoint/resume + rate-limited Gemini embeddings, and ship a multi-stage Dockerfile that bakes `data/index.sqlite` into the image. Everything else hangs off this.
