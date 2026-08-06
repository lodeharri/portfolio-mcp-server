# ADR 004: Stateful client + stateless server for chat persistence

- **Status**: Accepted
- **Date**: 2026-08-06
- **Change**: `003-playground-ui`
- **Deciders**: Harrison Rodriguez (solo), SDD design phase

## Context and Problem Statement

The chat surface needs conversation persistence: a recruiter sends a message, the agent replies, the recruiter asks a follow-up that references the first answer, the agent has context for both. The question is: where does the conversation history live?

Three failure modes must be designed out:

1. **GDPR / privacy / surveillance** — a portfolio piece that fingerprints recruiters (IP-based session keys, cookies, third-party analytics) reads as surveillance-y and contradicts the project's "honest, no-BS" ethos.
2. **Auth-less identity** — there is no login. Without an identity, the server cannot key conversation state without leaking the IP (which is a privacy problem and also dynamic, so it doesn't even work).
3. **Cross-device confusion** — recruiters on phone vs laptop expect separate conversations, not a shared thread that mixes the two contexts.

The natural alternatives split along two axes: server-persisted (DB row keyed by IP or session) vs client-persisted (`localStorage` / `IndexedDB`), and authenticated (login → user ID) vs anonymous (random UUID).

## Decision Drivers

- **D1**: No auth. The demo is for recruiters who don't have accounts and won't sign up. Auth is out of scope for `003-playground-ui`.
- **D2**: No IP-based tracking. The server has no business keying conversation state by IP.
- **D3**: No cookies. No third-party analytics. No tracking pixels.
- **D4**: Recruiter owns their data. A recruiter who clears `localStorage` should see a fresh conversation.
- **D5**: Cross-device isolation is honest. The same recruiter on phone vs laptop has two separate conversations; this is what they expect.
- **D6**: Zero Python LOC for persistence. The server has no DB schema, no in-memory map, no migration story. The persistence concern lives entirely in the browser.
- **D7**: Server restart is invisible. No state to lose; no warm-up time.

## Considered Options

### Option A — Stateful client + stateless server (chosen)

The browser owns the full conversation history in `localStorage`. Every `POST /chat/stream` request sends the entire `messages` array as JSON; the server receives the full history, invokes the agent, returns only the new assistant message; the client appends it on `data: [DONE]`. ~40 LOC of JavaScript in `playground/templates/chat.html`. Zero Python LOC.

```javascript
// playground/templates/chat.html (essence)
const NS = "mcp-playground-chat";
let sid = localStorage.getItem(NS + ":sid") ?? crypto.randomUUID();
localStorage.setItem(NS + ":sid", sid);

const hkey = `${NS}:${sid}:history`;
function load() { return JSON.parse(localStorage.getItem(hkey) || "[]"); }
function save(m) { localStorage.setItem(hkey, JSON.stringify(m)); }

// POST: full messages array
await fetch("/chat/stream", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ messages: load() }),
});

// On [DONE]: append one entry
save([...load(), { role: "assistant", content: accumulated }]);
```

**Pros**:
- **Zero Python LOC** for persistence. The server is genuinely stateless.
- **No IP-based tracking.** The `sid` is a random UUID generated on the recruiter's first visit; the server never sees it.
- **No cookies, no third-party analytics.** The server emits no `Set-Cookie` header on `/chat*` responses.
- **GDPR-clean.** No conversation data is persisted on the server. A server breach leaks no chat history.
- **Server restart is invisible.** No warm-up state to restore.
- **Cross-device isolation is honest.** Phone and laptop are separate conversations; recruiters don't expect otherwise.
- **Recruiter owns their data.** Clearing `localStorage` wipes the chat.

**Cons**:
- **Cross-device sync is absent.** A recruiter who switches devices mid-conversation loses context. Acceptable for a portfolio demo; documented in the UI as "each device keeps its own conversation".
- **`localStorage` quota (5 MB in most browsers).** A long conversation hits the cap; the script degrades to single-message mode with a notice flag. Mitigated by the try/catch around every `setItem`.
- **Private mode / `localStorage` blocked.** The script catches `SecurityError` and degrades to single-message mode.
- **Two tabs in the same profile share history.** The last tab to send a message wins. Acceptable for a portfolio demo; no locking required.

### Option B — Server-persisted keyed by random session UUID stored in a cookie (rejected)

Server keeps an in-memory `dict[uuid, list[messages]]`. Cookie holds the UUID. Same shape as Option A but with server state.

**Pros**:
- Cross-device sync is possible (UUID is portable).
- The server can enforce rate limits per-conversation, not just per-IP.

**Cons**:
- **Privacy** — the server holds recruiter conversations. GDPR data-controller obligations apply. The portfolio piece is now responsible for chat data.
- **Server restart loses conversations.** The in-memory map dies with the process. Persistence requires a DB; adds migration + schema concerns.
- **Memory cost** — every active conversation consumes RAM. With Fly.io's 256 MB machine cap, this is a real ceiling.
- **No real benefit for the demo.** Recruiters don't expect cross-device sync; they expect per-device isolation.

### Option C — Server-persisted keyed by client IP (rejected)

Server keeps an LRU `dict[ip, list[messages]]`. No cookie, no UUID — the IP is the key.

**Pros**:
- Zero client-side code. The browser sends an IP implicitly.

**Cons**:
- **Surveillance-y.** Keying chat history by IP without consent is exactly the pattern the EU's ePrivacy Directive scrutinizes. A portfolio piece that does this reads badly to recruiters who know the law.
- **Dynamic IPs break conversations.** A recruiter on mobile (carrier-grade NAT, rotating IPs) loses history every few minutes.
- **GDPR concerns.** IP addresses are personal data under GDPR. Keying chat state by IP makes the server a data controller for the chat content.
- **NAT/CGNAT confusion.** Multiple recruiters behind the same corporate NAT share a conversation. Embarrassing.

### Option D — No persistence at all (rejected)

Each `POST /chat/stream` carries only the current user message. The agent sees a one-turn conversation every time. No conversation history on either side.

**Pros**:
- Simplest possible implementation. ~10 LOC of JS.
- Truly stateless.

**Cons**:
- **Worse UX.** The recruiter can't ask "tell me more about the first project" without repeating the entire thread. This is the failure mode that makes the chat demo feel cheap.
- **Wastes the agent's tool budget.** The agent has to re-discover the project list on every turn.

## Decision

**Option A.** Stateful client + stateless server. `localStorage` holds the conversation; every `POST /chat/stream` sends the full history; the server invokes the agent with the full message list and returns only the new assistant message. The client appends on `data: [DONE]`.

The contract is enforced in two places:

- `chat-persistence` spec scenarios ("Full Messages Sent Per Request", "Server Holds No Chat State", "Graceful Degradation When localStorage Is Unavailable", "No Set-Cookie header on /chat/stream").
- `playground-ui` spec scenario ("Chat stream sends full messages array per request").

## Consequences

**Positive**:
- Zero server-side persistence code. Zero GDPR surface. Zero server state to lose on restart.
- Recruiter controls their data (clear `localStorage` → fresh chat).
- The Python codebase grows by zero LOC for persistence; the entire feature is ~40 LOC of JavaScript in the chat template.
- Fly.io autoscale-to-zero works without state-migration concerns.

**Negative**:
- Cross-device sync is absent. Documented in the UI: "each device keeps its own conversation".
- Long conversations hit `localStorage` quota (~5 MB); the script degrades gracefully.
- Two tabs in the same profile share history; the last writer wins.
- A future change that wants cross-device sync must re-evaluate this decision (Option B with explicit auth becomes the natural path).

## Compliance with rules

- `rules.apply.guidelines` → "Hexagonal architecture is mandatory" — satisfied; persistence is an HTTP adapter concern (the chat template), not an application concern.
- `rules.apply.guidelines` → "Never touch os.environ outside src/mcp_server/config.py" — satisfied; the server doesn't read or write any client-side state.
- `invariants` → "5-layer security model is mandatory" — preserved; Layer 3 sanitization happens per-token in the use case (ADR-005), so the server can stay stateless.

## Follow-ups

- In apply phase: write `tests/integration/test_chat_streaming.py` asserting no `Set-Cookie` header on `/chat/stream` responses.
- In apply phase: write `tests/integration/test_chat_persistence_contract.py` asserting the server holds no chat state across process restarts (assertion: spin up two `create_app()` instances, send identical requests, identical responses).
- In verify phase: confirm the recruiter demo experience on phone + laptop is "two separate conversations" (matches the documented contract).
