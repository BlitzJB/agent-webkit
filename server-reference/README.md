# agent-webkit reference server

FastAPI server implementing the agent-webkit wire protocol (`docs/wire-protocol.md`).
**Reference / example only — not shipped as part of the SDK.** Treat this as the canonical
implementation that documents the wire format by being it.

## Run (dev)

```sh
pip install -e ".[dev]"
python -m server.main --no-auth --port 8000
```

## Run (with auth)

```sh
AGENT_WEBKIT_TOKEN=secret python -m server.main --port 8000
```

## Architecture

- `server/main.py` — FastAPI app, endpoints, auth wiring.
- `server/session.py` — long-lived `Session` per `session_id`: holds the SDK client,
  inbound queue, event log, and pending-decision router. `SessionRegistry` owns lifecycle
  + idle eviction (5 min default).
- `server/sdk_bridge.py` — translates SDK messages → wire events; `can_use_tool` callback
  that turns permissions and AskUserQuestion into out-of-band SSE events awaiting a reply
  on a per-correlation-id `Future`.
- `server/event_log.py` — bounded ring buffer + multi-subscriber fan-out with
  `Last-Event-ID` resume.
- `server/models.py` — Pydantic models mirroring the TS types in `packages/core/src/types.ts`.
- `server/auth.py` — bearer-token auth (togglable).

## Swapping the SDK in tests

`create_app(sdk_factory=...)` accepts any async factory that returns an object satisfying
the `SDKClient` Protocol. The mock SDK in `tests/fake_claude_sdk/` is a drop-in.
