# ADR Index — 003-playground-ui

| # | Decision | Status |
|---|---|---|
| [001](001-htmx-over-react.md) | HTMX 1.9.10 vendored at `playground/static/htmx.min.js` — not React, Vue, or a SPA framework | Accepted |
| [002](002-fastapi-native-sse.md) | FastAPI native `EventSourceResponse` — not `sse-starlette` pip dep | Accepted |
| [003](003-langgraph-stream-messages.md) | LangGraph `stream_mode="messages"` with `langgraph>=0.2,<2.0` major-version pin | Accepted |
| [004](004-stateful-client-stateless-server.md) | Stateful client (`localStorage`) + stateless server for chat persistence | Accepted |
| [005](005-per-token-sanitization-in-use-case.md) | Per-token sanitization in `AskPortfolioUseCase.astream` — not the HTTP middleware | Accepted |

## Cross-references

- [design.md](../design.md) — the parent design document
- [proposal.md](../proposal.md) — the approved proposal
- [specs/](../specs/) — the five delta specs (playground-ui, agent-streaming, chat-persistence, sanitizer-skip-list, dockerfile-playground, llm-prompt-discipline)
