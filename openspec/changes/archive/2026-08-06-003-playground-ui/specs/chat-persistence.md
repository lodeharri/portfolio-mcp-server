# chat-persistence — Delta Specification

## Purpose

The conversation-state contract for the `/chat` tab. Per Decision #11 the
server is **stateless** for chat: there is no DB row, no in-memory map, no
cookie, and no IP-based tracking. The browser owns the full conversation
history in `localStorage` and sends the entire `messages` array with every
`POST /chat/stream` request. The agent receives the full history and
returns only the new assistant message; the client appends it on
`data: [DONE]`.

This avoids three problems that come with server-side conversation state:
(1) no user identity → no conversation ID → nothing to track (GDPR /
dynamic-IP / surveillance concerns in a portfolio piece), (2) cross-device
sync is honest — recruiters know their chat is local, (3) zero Python LOC
for persistence (the entire feature is ~40 LOC of JS in
`playground/templates/chat.html`).

## Schema / Interface

```javascript
// playground/templates/chat.html — inline <script> (or chat.js static)
const SESSION_KEY = "mcp-playground-chat";

// On first visit
let sessionId = localStorage.getItem(SESSION_KEY + ":sid");
if (!sessionId) {
  sessionId = window.crypto.randomUUID();
  localStorage.setItem(SESSION_KEY + ":sid", sessionId);
}

// History persistence
const historyKey = `${SESSION_KEY}:${sessionId}:history`;
function loadHistory() {
  try { return JSON.parse(localStorage.getItem(historyKey) || "[]"); }
  catch { return []; }
}
function saveHistory(messages) {
  try { localStorage.setItem(historyKey, JSON.stringify(messages)); }
  catch (e) { showPersistenceNotice(e); }
}

// On /chat/stream request
const response = await fetch("/chat/stream", {
  method: "POST",
  headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
  body: JSON.stringify({ messages: loadHistory() }),
});

// On data: [DONE] event
saveHistory([...loadHistory(), { role: "assistant", content: accumulatedText }]);
```

## ADDED Requirements

### Requirement: Server Holds No Chat State

The server MUST NOT persist conversation history for `/chat` requests in
any form. There MUST be no database table, no in-memory map keyed by
session, no cookie, and no IP-based key derived from `request.client.host`.
The server is allowed to keep the in-process rate-limit map (slowapi,
Layer 5) because that is request-shaping, not conversation-state.

#### Scenario: Server emits no persistence on /chat/stream

- GIVEN the same client IP sends 5 consecutive `/chat/stream` requests
- WHEN each request completes
- THEN the server MUST NOT have written any new rows to any DB
- AND MUST NOT have created any new module-level state keyed by the
  client IP or any session id
- AND a process restart MUST cause the next request to behave
  identically (server has amnesia between requests).

#### Scenario: Rate-limit state is the only per-IP server memory

- GIVEN slowapi is wired (Layer 5)
- WHEN the 31st `/chat/stream` request arrives from the same IP within
  60 seconds
- THEN the response MUST be HTTP 429 (the request-shaping state is
  allowed)
- AND the server MUST NOT store any conversation content keyed by that IP.

### Requirement: Client Persists History in localStorage

The chat UI MUST persist the full conversation history in the browser's
`localStorage` under a key namespaced by a random per-session UUID. The
session UUID MUST be generated via `window.crypto.randomUUID()` on the
recruiter's first visit (no PII, no IP derivation, no cookie consent
gate).

#### Scenario: Session UUID is generated on first visit

- GIVEN the recruiter opens `/chat` for the first time in a fresh browser
  profile (no `mcp-playground-chat:*` keys present)
- WHEN the chat script runs
- THEN it MUST call `window.crypto.randomUUID()`
- AND MUST store the result under the key `mcp-playground-chat:sid`
- AND MUST use that UUID as the namespace prefix for the history key.

#### Scenario: History is restored on page reload

- GIVEN the recruiter reloads `/chat` after sending two messages
- WHEN the chat script runs
- THEN it MUST read the stored history
- AND MUST render every prior turn in the visible transcript BEFORE
  accepting new input.

#### Scenario: History key is namespaced by session UUID

- GIVEN two browser profiles generate different session UUIDs
- WHEN each profile writes its own history
- THEN the localStorage keys MUST differ (the UUID is part of the key)
- AND reading one profile's history MUST NOT leak into the other.

### Requirement: Full Messages Sent Per Request

Every `POST /chat/stream` request from the client MUST include the entire
conversation history as a JSON body. The server MUST NOT be expected to
reconstruct prior turns from any prior state — it MUST treat the
`messages` array as the only source of context.

#### Scenario: First turn sends a length-1 messages array

- GIVEN the recruiter has not chatted before (history is empty)
- WHEN the recruiter sends their first message
- THEN the POST body MUST contain `{"messages": [{"role": "user", "content": "..."}]}`
- AND `messages.length` MUST equal 1.

#### Scenario: Nth turn sends a length-N messages array

- GIVEN the recruiter has sent 3 prior turns (history has 6 entries:
  user, assistant, user, assistant, user, assistant)
- WHEN the recruiter sends a 4th user message
- THEN the POST body MUST contain `messages` with length 7 (6 prior + 1
  new user)
- AND every prior turn MUST appear with its original `role` and `content`.

#### Scenario: Server response carries only the new assistant message

- GIVEN the agent finishes and yields a 50-token reply
- WHEN the SSE stream terminates with `data: [DONE]\n\n`
- THEN the body MUST contain exactly one assistant message worth of
  tokens (concatenated from the SSE events)
- AND MUST NOT echo the prior turns back to the client.

### Requirement: Assistant Reply Appended on DONE Event

The client MUST accumulate token chunks until it receives the
`data: [DONE]\n\n` sentinel, then MUST append a single
`{role: "assistant", content: <accumulated>}` entry to the stored history
and to the visible transcript. If the connection drops before `data:
[DONE]` is received, the client MUST NOT append a partial assistant
message.

#### Scenario: DONE event triggers a single append

- GIVEN the agent streams 5 tokens and then `data: [DONE]`
- WHEN the EventSource fires the DONE event
- THEN the client MUST append exactly one entry to `localStorage`
- AND the entry MUST have `role: "assistant"` and the full concatenated
  content.

#### Scenario: Connection drop before DONE does not append

- GIVEN the network drops after 3 of 5 tokens
- WHEN no DONE event arrives within the read timeout
- THEN the client MUST NOT modify `localStorage`
- AND MUST render an inline "connection lost, retry?" affordance (no
  partial assistant message in storage).

### Requirement: Graceful Degradation When localStorage Is Unavailable

When `localStorage` is unavailable (private browsing mode, quota exceeded,
or browser policy block), the chat MUST continue to function as a single-
message-at-a-time tool. The UI MUST show a one-line notice explaining that
the conversation will not persist across reloads, and MUST NOT crash.

#### Scenario: localStorage throws on write

- GIVEN the browser blocks `localStorage.setItem` (private mode or quota)
- WHEN the chat script tries to save the conversation
- THEN it MUST catch the exception
- AND MUST NOT propagate the error to the user as an uncaught exception
- AND MUST display the notice "Conversations in this browser are not
  saved between reloads."

#### Scenario: Single-message mode still works without persistence

- GIVEN `localStorage` is unavailable
- WHEN the recruiter sends a message
- THEN the client MUST POST only the current user message (no prior
  turns) to `/chat/stream`
- AND the assistant reply MUST render in the transcript
- AND a page reload MUST leave the transcript empty (no prior turns).

### Requirement: No Tracking, No Cookies, No IP-Based Keys

The chat surface MUST NOT use cookies, MUST NOT fingerprint the browser
beyond the existing session UUID in `localStorage`, and MUST NOT use
`request.client.host` for any session-shaping logic. The only client-
side identifier is the `localStorage` UUID; the server does not see it.

#### Scenario: No Set-Cookie header on /chat/stream

- GIVEN any `/chat/stream` request
- WHEN the server responds
- THEN the response MUST NOT include a `Set-Cookie` header
- AND MUST NOT include any tracking pixel, beacon, or analytics ping.

#### Scenario: Server-side logs do not contain the session UUID

- GIVEN the recruiter sends `/chat/stream` requests
- WHEN the audit log is inspected
- THEN no log line MUST contain the `localStorage` UUID value
- AND no log line MUST contain the full message content (only the event
  type and a redacted length).

## Error / Edge Cases

- `JSON.parse` failure on a corrupted history entry: client MUST treat
  the history as empty and continue (no crash, no wipe of `localStorage`
  without explicit user action).
- Quota exceeded while saving: degrade to single-message mode (per the
  Graceful Degradation requirement) and persist the notice flag so the
  next page load doesn't re-attempt a doomed write.
- Clock skew on `crypto.randomUUID()`: not a concern (UUID v4 is
  random, not timestamp-based).
- Two browser tabs in the same profile: both share the same
  `localStorage` history; the LAST tab to send a message wins (acceptable
  for a portfolio demo — no locking or broadcast channel required).

## Test Scenarios

| Scenario | Required because |
|---|---|
| Server restart drops all conversation state (memory test) | Stateless-server contract (Decision #11) |
| `localStorage` round-trip preserves history across reloads | Client-side persistence |
| `/chat/stream` request body contains the full messages array | Full-history contract |
| `data: [DONE]` triggers exactly one append per assistant turn | Append-on-DONE contract |
| Private-mode browser degrades to single-message mode | Graceful degradation |
| No `Set-Cookie` header in `/chat` or `/chat/stream` responses | Privacy invariant |
