# agent-webkit-server

Server-side toolkit for exposing the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) over HTTP+SSE. One Python package; bundled adapters cover the common deployment shapes.

## Install

```bash
pip install agent-webkit-server                # core only
pip install "agent-webkit-server[fastapi]"     # + FastAPI HTTP adapter
pip install "agent-webkit-server[postgres]"    # + PgSessionStore
pip install "agent-webkit-server[fastapi,postgres]"
```

## Core

Framework-agnostic primitives — no FastAPI, no asyncpg until you opt in.

```python
from agent_webkit_server import PROTOCOL_VERSION
from agent_webkit_server.session import SessionRegistry, SessionConfig
from agent_webkit_server.event_log import EventLog
from agent_webkit_server.sdk_bridge import (
    PermissionRouter,
    build_can_use_tool,
    translate_sdk_messages,
)
```

These pieces are what you'd otherwise reimplement: per-session inbound queue + receive-loop, append-only event log with multi-subscriber fan-out, permission/AskUserQuestion correlation router, the `can_use_tool` wiring, and the SDK→wire-event translator.

## FastAPI adapter

```python
from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig

app = create_app(auth=AuthConfig.from_env())
```

Exposes:
- `POST /sessions`
- `GET /sessions/{id}/stream` (SSE, with `Last-Event-ID` resume)
- `POST /sessions/{id}/input` (every inbound message type)
- `DELETE /sessions/{id}`

## Postgres adapter (`PgSessionStore`)

Plug into the SDK directly via `ClaudeAgentOptions(session_store=...)`:

```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from agent_webkit_server.adapters.pg_session_store import PgSessionStore

store = await PgSessionStore.connect("postgresql://...")
options = ClaudeAgentOptions(session_store=store, resume=session_id)
client = ClaudeSDKClient(options=options)
```

Passes the SDK's published `run_session_store_conformance` suite plus adapter-specific contracts (uuid idempotency, concurrent-append serialization, multi-tenant isolation).

## Wire protocol

Documented in [`docs/wire-protocol.md`](../../docs/wire-protocol.md). The Pydantic models in `agent_webkit_server.models` are the canonical schema.
