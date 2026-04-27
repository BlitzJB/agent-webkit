# agent-webkit-server

Python server library that holds long-lived [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) sessions and exposes them over HTTP+SSE.

```bash
pip install "agent-webkit-server[fastapi]"
```

Optional Postgres adapter for failover:

```bash
pip install "agent-webkit-server[fastapi,postgres]"
```

## Use

```python
from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig

app = create_app(auth=AuthConfig.from_env())
```

Run with any ASGI server:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

What's mounted:

- `POST /sessions` — create a session.
- `GET /sessions/{id}/stream` — SSE event stream (multi-subscriber, resumable via `Last-Event-ID`).
- `POST /sessions/{id}/input` — send messages, replies, interrupts.
- `DELETE /sessions/{id}` — graceful teardown.

What it handles:

- Long-lived `ClaudeSDKClient` per session — one expensive `connect()` per session, not per turn.
- `can_use_tool` callback turned into a clean wire-level RPC.
- `AskUserQuestion` routed as a first-class event.
- Multi-subscriber SSE fan-out with bounded ring buffer for resume.
- Idle eviction.
- Pluggable session storage (in-memory by default; Postgres adapter included).

## Docs

- [FastAPI guide](https://agent-webkit-docs.vercel.app/docs/guides/backend-fastapi)
- [Custom adapter](https://agent-webkit-docs.vercel.app/docs/guides/backend-custom-adapter) — Starlette, Litestar, raw ASGI.
- [Postgres sessions](https://agent-webkit-docs.vercel.app/docs/guides/postgres-sessions)
- [Server API reference](https://agent-webkit-docs.vercel.app/docs/reference/server-api)
- [Wire protocol](https://agent-webkit-docs.vercel.app/docs/reference/wire-protocol)

## License

MIT
