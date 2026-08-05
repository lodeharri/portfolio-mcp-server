# Proposal: 001-bootstrap — Foundation, Security Layers, Preindex Pipeline

## Intent
Greenfield scaffold. Without it, the 5-layer security model is unwritten and the vector index cannot be built. Minimum that makes every later change demoable.

## Scope

### In Scope
1. `pip install -e ".[dev]"` (deps in `pyproject.toml`).
2. FastAPI factory `src/mcp_server/app.py`: `/healthz` 200+version, reads `$PORT`, mounts FastMCP sub-app.
3. Typed config `src/mcp_server/config.py` — **only** module reading `os.environ`.
4. `src/mcp_server/security/`: `manifest_loader` · `gitleaks` (subprocess → `blocked|flagged|clean`) · `sanitizer` (AWS/GitHub/OpenAI/Gemini/generic regex, redacts+logs) · `rate_limit` (slowapi 30 req/min/IP) · `audit` (structlog JSON).
5. `src/mcp_server/interfaces/cli/preindex.py` (CLI entry under `interfaces/cli/`): manifest-driven, 1500/200 chars, sha256 hash cache, Gemini embeddings w/ 0.1s sleep, per-chunk gitleaks (block high / flag medium), writes `data/index.sqlite`.
6. Multi-stage `Dockerfile`: builder runs `preindex.py` (`GEMINI_API_KEY` via BuildKit `--secret`); runtime ships baked index as non-root UID 10001; `HEALTHCHECK` on `/healthz`; `<150 MB`.
7. RED-first tests: manifest loader, gitleaks (mocked), sanitizer (table-driven), slowapi, preindex (mock Gemini), `/healthz` integration. Coverage ≥60%.
8. CI green: `test.yml` / `lint.yml` / `secret-scan.yml` already exist — verify pass.

### Out of Scope
Deferred: 6 MCP tools (`002-mcp-tools`) · Playground UI (`003-playground-ui`) · Streaming chat (`004-chat-tab`) · Fly.io deploy (`005-deploy`).

## Capabilities

### New
- `app-bootstrap` — FastAPI factory, `/healthz`, port-agnostic binding, MCP sub-app mount.
- `security-layers` — manifest loader, gitleaks wrapper, sanitizer, rate limiter, audit log.
- `preindex-pipeline` — chunking, sha256 cache, rate-limited embeddings, per-chunk gitleaks, sqlite-vec build.
- `container-image` — multi-stage Dockerfile, baked index, non-root, healthcheck.

### Modified
None — greenfield.

## Approach
Hexagonal (`domain` ⟵ `application` ⟵ `infrastructure`); `finance-coach-latam` as reference. Strict TDD. Index baked at build → deploy-agnostic, zero runtime cold-start.

## Risks

| Risk | L | Mitigation |
|-----|---|------------|
| gitleaks binary missing at build | M | Tarball in builder |
| Per-chunk scan >200 ms | M | Cache by `chunk_hash` |
| Image >150 MB | M | `--no-cache-dir`; drop build-essentials |
| `GEMINI_API_KEY` leaks in image | L | BuildKit `--secret` |
| Missed ≥60% coverage | L | RED-first; `fail_under = 60` |

## Rollback
Additive. Revert merge → pre-scaffold state. No prior prod image. If broken: delete branch, revert, reopen.

## Dependencies
`openspec/config.yaml` · `pyproject.toml` · `config/projects.manifest.yaml` (read-only) · gitleaks binary.

## Success Criteria
- [ ] `pip install -e ".[dev]"` clean · `pytest -q` passes; coverage ≥60%
- [ ] `/healthz` returns 200+version on `uvicorn ... --port 8080`
- [ ] `python scripts/preindex.py --mock-gemini` runs end-to-end
- [ ] `docker build` succeeds; image <150 MB
- [ ] `gitleaks detect --redact` + pre-commit + CI all green

## Cost Discipline
Fly.io ~$2.02/mo 256 MB (free tier gone Oct 2024). Gemini free RPM honored (0.1s sleep + sha256 cache). GitHub Actions 2000 min/mo free. No paid services added.
