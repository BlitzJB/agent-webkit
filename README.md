# agent-webkit

A JS SDK + Python server library that exposes the
[Claude Agent SDK (Python)](https://code.claude.com/docs/en/agent-sdk/python) over HTTP+SSE,
so web/Node clients can drive an agent session with full streaming, steering, permission
approvals, and `AskUserQuestion` handling — without re-implementing the SDK semantics on
the client.

## Packages

| Package                       | Purpose                                                                      |
| ----------------------------- | ---------------------------------------------------------------------------- |
| [`@agent-webkit/core`](packages/core)   | L1 vanilla JS SDK. Isomorphic (browser/Node/Deno/Bun). No React, no UI.       |
| [`@agent-webkit/react`](packages/react) | L2 React abstractions. `useAgentSession()` with delta reconciliation.        |
| [`agent-webkit-server`](packages/agent-webkit-server) | Python library: core engine + bundled adapters (FastAPI, Postgres `SessionStore`). |
| [`examples/server-fastapi/`](examples/server-fastapi) | 30-line reference deployment of the FastAPI adapter. |
| [`tools/record-fixtures/`](tools/record-fixtures) | Drives the real SDK to produce JSONL ground-truth fixtures.       |
| [`tests/fake_claude_sdk/`](tests/fake_claude_sdk) | Drop-in mock that replays fixtures.                                |
| [`examples/chat-demo/`](examples/chat-demo) | Vite + React end-to-end demo.                                          |

## Architecture in one paragraph

The server library holds **one long-lived `ClaudeSDKClient` per session** (because
`connect()` spawns the Claude Code CLI subprocess — fork-per-message would be brutal).
It exposes a tiny HTTP+SSE wire protocol (`docs/wire-protocol.md`). When the SDK calls
`can_use_tool` mid-`query()`, the server registers a `Future` keyed by `correlation_id`,
emits a `permission_request` SSE event, and awaits the matching `permission_response` from
the client. `AskUserQuestion` is hooked the same way but routed through a dedicated event
type so L2 can give it first-class UI. Sessions idle-evict after 5 min.

The L1 client is a typed transport — POST helpers + an SSE async-iterable with auto-reconnect
via `Last-Event-ID`. L2 layers a reducer-based React hook on top: streaming-delta →
complete reconciliation by `message_id`, race resolution UI states, typed callbacks.

## Quick start

Requires `pnpm` ≥ 9 and Python ≥ 3.10.

```sh
# Server
pip install "./packages/agent-webkit-server[fastapi]"
python examples/server-fastapi/main.py --no-auth

# Web (separate terminal)
pnpm install
pnpm --filter @agent-webkit/chat-demo dev
```

Open http://localhost:5173 — the demo proxies `/sessions` to the FastAPI server.

## Wire protocol

See [`docs/wire-protocol.md`](docs/wire-protocol.md). It is the canonical source of truth
across `@agent-webkit/core` (TS), `agent-webkit-server` (Pydantic), and any third-party
implementations.

`protocol_version = "1.0"`.

## Testing strategy

Four tiers:

1. **Unit (mock SDK, fast, every commit)**: server logic — queue handling, SSE event
   ordering, correlation-ID lifecycle, resume-from-seq, permission RPC roundtrip,
   race resolution (409), idle eviction, multi-subscriber fan-out.
   Run: `pytest tests/unit tests/contract`.
2. **Contract (mock SDK, schema validation)**: every wire payload validated against typed
   Pydantic schemas. Run: same as unit.
3. **Property (Hypothesis)**: invariants for `EventLog`, `PermissionRouter`, the SDK→wire
   translator, and the Postgres `SessionStore` adapter — covers spaces example tests can't
   enumerate. Run: `pytest tests/properties` (Postgres examples skip without `PG_DSN` /
   `PG_USE_DEFAULT=1`).
4. **Integration (real SDK, gated, nightly)**: end-to-end smoke + HTTP+SSE round-trip,
   gated on `CLAUDE_CODE_OAUTH_TOKEN`. Run: `pytest tests/integration`.

For L1 / L2 / SSE parser, run `pnpm test` from the repo root.

## Key design decisions

- **One SDK client per session, not per request.** `connect()` spawns a subprocess; we
  don't re-pay that cost on every turn.
- **Permission/question callbacks block the SDK's `query()`.** Handler converts to SSE +
  awaits a Future. Don't tear down the client while a callback is pending.
- **Sessions are JSONL on local disk** (per the SDK). v1 is single-host. Cross-host resume
  needs a `SessionStore` adapter — out of scope.
- **`interrupt()` does not drain the buffer.** The receive loop must finish draining before
  the next `query()`.
- **First reply wins** for permission/question/hook decisions; loser gets HTTP 409.
- **Multi-subscriber fan-out** with independent SSE cursors per subscriber, backed by a
  bounded ring buffer (default 1000 events).
- **Auth is configurable.** `--no-auth` for local/dev; bearer token otherwise. L1 picks
  fetch-based SSE automatically when a token is provided.

## Out of scope for v1

- Cross-host session resume (mention `SessionStore` adapter as a known path).
- Hook-decision-request UI plumbing (server emits the event; L2 stub only).
- Authentication providers beyond static bearer token.
- Rate limiting / backpressure beyond a bounded inbound queue.
