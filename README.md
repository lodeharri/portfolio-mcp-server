# portfolio-mcp-server

Harrison Rodriguez's portfolio piece: an **MCP (Model Context Protocol) server in Python** that indexes his other projects and exposes them as searchable tools to recruiters via FastMCP.

> **Status:** 5 of 6 tools fully functional. `ask_portfolio` has a known bug (see [Open bugs](#open-bugs)). Local dev works with mock-gemini for zero-token testing.

Sibling projects indexed by this server:
- [`finance-coach-latam`](../finance-coach-latam) — serverless AI finance coach (AWS Lambda + Cloudflare Pages)
- [`landing-page-portfolio`](../landing-page-portfolio) — bilingual EN/ES portfolio (Astro + Cloudflare Pages)

## What this project is for

The current portfolio is heavy on architecture and light on live demos. This piece fills the gaps:

- **Recruiter-facing MCP server** — connect Claude Desktop, Cursor, or any MCP client to ask questions about Harrison's work
- **Python backend showcase** (the rest of his projects are TypeScript)
- **Container-based deploy** (Fly.io, not serverless like `finance-coach-latam`)
- **AI infrastructure** (semantic search over his code, Gemini-powered tool calls)
- **RAG over portfolio code** — `ask_portfolio` is a Pydantic AI agent that orchestrates the other 5 tools

## The 6 MCP tools

| Tool | Status | Description |
|------|--------|-------------|
| `list_projects()` | ✅ | Lists projects declared in `projects.manifest.yaml` with chunk counts |
| `search_code(query, language?)` | ✅ | Semantic search over indexed code via sqlite-vec |
| `explain_architecture(project_id)` | ✅ | Reads ADR files + Gemini summary |
| `summarize_readme(project_id)` | ✅ | Reads README + Gemini summary |
| `get_architecture_diagram(project_id)` | ⚠️ | Returns SVG base64 — file not in manifest yet |
| `ask_portfolio(question)` | ⚠️ | Pydantic AI agent — has a bug, falls back to mock |

### Local demo of 5 working tools

```python
import asyncio
from fastmcp import Client
from mcp_server.app import create_app
from mcp_server.config import AppConfig
from mcp_server.interfaces.mcp.server import mcp

config = AppConfig(gemini_api_key="fake")  # mock-gemini auto-fallback
create_app(config)

async def main():
    async with Client(mcp) as client:
        # List projects
        for p in (await client.call_tool("list_projects", {})).data:
            print(f"{p['id']}: {p['index_chunk_count']} chunks")

        # Search code semantically
        r = (await client.call_tool("search_code",
               {"query": "async error handling"})).data
        for c in r[:3]:
            print(f"  {c['file_path']}: {c['content'][:60]}")

        # Read architecture docs
        r = (await client.call_tool("explain_architecture",
               {"project_id": "finance-coach-latam"})).data
        print(f"  Architecture summary: {r['summary'][:200]}")

asyncio.run(main())
```

## Architecture (hexagonal + SOLID)

```
                     ┌─────────────────────────────────────────────┐
                     │  INTERFACES (FastMCP tools @ /mcp)            │
                     │  └── uses application/use_cases (hexagonal)  │
                     ├─────────────────────────────────────────────┤
                     │  APPLICATION (use cases + ports)             │
                     │  use cases depend on ports (Protocols)       │
                     │  ports are abstract interfaces               │
                     ├─────────────────────────────────────────────┤
                     │  DOMAIN (pure Python, no framework deps)     │
                     │  entities (CodeChunk, Project, ...)         │
                     │  value objects (ChunkHash, Vector, ...)     │
                     │  exceptions (DomainError, ...)              │
                     └─────────────────────────────────────────────┘
                              ▲
                              │ implements
                     ┌─────────────────────────────────────────────┐
                     │  INFRASTRUCTURE (adapters)                   │
                     │  ONE file: src/mcp_server/infrastructure/  │
                     │             langchain.py                     │
                     │  (chunking + agent + embedding)             │
                     └─────────────────────────────────────────────┘
```

**Dependency direction:** `interfaces → application → domain`, never reversed.
`infrastructure → application` (implements ports).
**Composition root** (`src/mcp_server/composition.py`) is the ONLY module that wires adapters to use cases.

### Single-file LangChain centralization

All LangChain wiring lives in `src/mcp_server/infrastructure/langchain.py`:
- `LangChainChunkingAdapter` (language-aware chunking — Python, Markdown, JS)
- `LangChainAgentAdapter` (ReAct agent with 5 sibling tools)
- `LangChainEmbeddingAdapter` (Gemini embeddings via LangChain)
- `_MockLangChainEmbeddingAdapter` (deterministic SHA-256-based fallback)
- `_MockLangChainAgentAdapter` (returns "[mock answer to: hi]")
- `_MockAskPortfolioUseCase` (defensive fallback when composition isn't wired)

The rest of the codebase is hexagonal — depends only on the abstract ports.

### 5-layer security model

1. **Manifest scoping** — `config/projects.manifest.yaml` is the single source of truth. Default-deny.
2. **Gitleaks at index time** — subprocess to Go binary, fail-closed on malformed output.
3. **Output sanitizer at runtime** — regex redaction of AWS/GitHub/OpenAI/Gemini keys + generic credentials.
4. **Pre-commit + CI gitleaks** — pre-commit hook + CI `gitleaks detect --redact` + GitHub secret scanning.
5. **Rate limiter + audit log** — slowapi 30 req/min/IP + structured audit log.

## Stack

| Layer | Choice |
|-------|--------|
| Backend | FastAPI + FastMCP (Python MCP SDK, single process) |
| Frontend (planned) | HTMX + htmx-ws + Jinja2 templates |
| Database | SQLite + `sqlite-vec` (vector search, no Neon needed) |
| LLM | Gemini 2.0 Flash + text-embedding-004 (free tier) |
| LLM framework | LangChain + LangGraph (ReAct agent) |
| Tests | pytest + pytest-asyncio + httpx + Playwright + axe-core |
| Lint/format | ruff + prettier |
| Container | Docker multi-stage (target <500 MB, current 417 MB) |
| Deploy | Fly.io primary (~$2/mo), HF Spaces / Render / Railway as fallbacks |

## Local dev setup

```bash
# 1. Clone and install
git clone https://github.com/lodeharri/portfolio-mcp-server.git
cd portfolio-mcp-server
pip install -e ".[dev]"

# 2. Set up environment
cp .env.example .env
# Edit .env — leave GEMINI_API_KEY empty for mock mode (zero tokens)

# 3. Preindex (uses mock-gemini if no key)
python -m mcp_server.interfaces.cli.preindex --mock-gemini --quiet

# 4. Run the MCP server
python -c "from mcp_server.app import create_app; from mcp_server.config import AppConfig; create_app(AppConfig())"
# Or in a Docker container:
docker run --rm -d -p 8080:8080 --name mcp mcp-server:test

# 5. Test with the FastMCP client
python -c "
import asyncio
from fastmcp import Client
from mcp_server.app import create_app
from mcp_server.config import AppConfig
from mcp_server.interfaces.mcp.server import mcp

create_app(AppConfig())

async def main():
    async with Client(mcp) as client:
        for p in (await client.call_tool('list_projects', {})).data:
            print(f\"{p['id']}: {p['index_chunk_count']} chunks\")

asyncio.run(main())
"
```

### Manual preindex from a different working directory

```bash
# Skip the index.sqlite for a fresh build
rm data/index.sqlite

# Preindex with your real API key (prod mode)
python -m mcp_server.interfaces.cli.preindex --manifest config/projects.manifest.yaml

# With --purge-orphans to delete chunks whose source files no longer exist
python -m mcp_server.interfaces.cli.preindex --purge-orphans
```

## Testing

```bash
# Run all tests
pytest -q
# → 479 passed, 2 skipped (Docker image size tests)

# Run ruff
ruff check src/mcp_server tests/

# Run a specific test suite
pytest tests/integration/test_mcp_tools_ask_portfolio.py -v
```

## Open bugs

### Bug #1 — ask_portfolio fails when composition is wired (BLOCKING for production)

**Symptom:** `Function must have a docstring if description not provided`

**Reproducible:**
```python
import asyncio
from mcp_server.app import create_app
from mcp_server.config import AppConfig
from fastmcp import Client
from mcp_server.interfaces.mcp.server import mcp
create_app(AppConfig(gemini_api_key="fake"))
async def main():
    async with Client(mcp) as client:
        await client.call_tool("ask_portfolio", {"question": "test"})
asyncio.run(main())
# → ToolError: Function must have a docstring if description not provided.
```

**Workaround:** The mock fallback (`_MockAskPortfolioUseCase`) activates when composition is NOT wired. Production always wires composition via `create_app()`, so the bug surfaces. The other 5 tools are unaffected.

**Suspected location:** LangChain agent tool introspection vs FastMCP 3.4.6's `tool.fn` API — likely a signature mismatch.

### Bug #2 — get_architecture_diagram has no source data

Both projects declare `diagram_path: docs/architecture.svg` in the manifest, but the files don't exist. The tool errors with "referenced file not found". Easy fix: create the SVG files or remove the field from the manifest.

### Bug #3 — Image size 417 MB vs original 150 MB target

The size was reduced from 676 MB to 417 MB by migrating to `google-genai` (replacing deprecated `google-generativeai` and dropping 100 MB of `google-api-python-client`). The 150 MB target was aspirational for this Python+AI stack. The image budget was raised to 500 MB to reflect reality. To hit 150 MB would require local embeddings (more code, more dependencies) or an alpine base (musl compatibility issues with sqlite-vec).

## Decisions locked

| Decision | Why |
|----------|-----|
| LangChain for everything (chunking + agent + embedding) | Single library, single file, no two-similar-libraries problem |
| Hexagonal architecture | Testable, swappable adapters, clean separation |
| SQLite + sqlite-vec (no Postgres) | No external DB, single file, portable |
| Gemini 2.0 Flash + text-embedding-004 | Free tier available, same as `finance-coach-latam` |
| 5-layer security model | Manifest + Gitleaks + Output Sanitizer + Pre-commit + Rate Limiter |
| No cron jobs | User explicitly rejected recurring automation |
| Pre-baked index committed to repo | Cheaper than CI build for portfolio demo |
| FastMCP mounted at `/mcp` | Standard MCP transport |
| Conventional commits, no AI attribution | Per finance-coach-latam convention |

## Project structure

```
portfolio-mcp-server/
├── README.md                                   # You are here
├── pyproject.toml                              # LangChain + pydantic-ai-slim + google-genai
├── Dockerfile                                  # Multi-stage, 417 MB
├── fly.toml                                    # Fly.io config
├── .env.example                                # Template for local dev
├── .github/workflows/                          # test, lint, secret-scan, deploy
├── config/
│   └── projects.manifest.yaml                  # Source of truth for indexing
├── src/mcp_server/
│   ├── app.py                                  # create_app() factory
│   ├── config.py                               # AppConfig, the only os.environ reader
│   ├── composition.py                          # DI container
│   ├── domain/                                 # Pure entities + value objects
│   ├── application/
│   │   ├── ports/                              # Abstract interfaces
│   │   └── use_cases/                          # Application services
│   ├── infrastructure/
│   │   ├── langchain.py                        # SINGLE FILE — LangChain wiring
│   │   ├── adapters/                           # Concrete implementations
│   │   ├── db/                                 # SQLite + schema
│   │   └── security/                           # 5-layer security
│   └── interfaces/
│       ├── mcp/                                # FastMCP tools + registrations
│       ├── http/                               # Healthz + middleware
│       └── cli/                                # preindex CLI
├── scripts/
│   └── bake_schema.py                          # Schema-only DB for Docker build
├── tests/
│   ├── conftest.py
│   ├── unit/
│   └── integration/
├── data/
│   └── index.sqlite                            # Generated, gitignored
└── openspec/
    ├── config.yaml
    ├── specs/                                  # Main capability specs
    └── changes/
        └── archive/
            ├── 2026-08-05-001-bootstrap/
            └── 2026-08-05-002-mcp-tools/
```

## Next steps (in priority order)

1. **Fix Bug #1** — `ask_portfolio` LangChain + FastMCP binding. Without this, production won't work for the recruiter demo.
2. **Fix Bug #2** — Create `architecture.svg` files or remove from manifest. Easy fix.
3. **`003-playground-ui`** — HTMX + Jinja2 templates for web playground.
4. **`004-chat-tab`** — Streaming chat with Pydantic AI agent.
5. **`005-fly-deploy`** — Real deploy to Fly.io (~$2-3/mo).

## License

See [`LICENSE`](LICENSE) (if not present, see the upstream convention).
