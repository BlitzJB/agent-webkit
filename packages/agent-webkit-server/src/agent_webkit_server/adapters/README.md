# Optional adapters

Pluggable add-ons for the Claude Agent SDK. None of these are imported by the core agent-webkit reference server — install them only if you need the feature.

## `PgSessionStore`

Postgres-backed `SessionStore` adapter. Mirrors the CLI's per-session JSONL transcripts to Postgres and serves store-backed resume so any worker can rehydrate any session.

### Install

```bash
pip install asyncpg
```

### Use

```python
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from server.extras.pg_session_store import PgSessionStore

store = await PgSessionStore.connect("postgresql://user:pass@host/db")
options = ClaudeAgentOptions(
    session_store=store,
    resume=session_id,           # optional — store-backed resume
)
client = ClaudeSDKClient(options=options)
```

The schema is created on first connect (idempotent `CREATE TABLE IF NOT EXISTS`).

`project_key` defaults to the sanitized cwd; for multi-tenant deployments set it to a tenant id (the SDK passes it through verbatim).

### Run the integration suite

```bash
docker compose -f docker-compose.test.yml up -d
PG_USE_DEFAULT=1 pytest tests/integration -q
```

Covers the SDK's published `run_session_store_conformance` suite (14 contracts) plus adapter-specific ITs: uuid idempotency under retry, concurrent appends to the same session, cross-instance reconnect, multi-tenant isolation under load, and the `summary.mtime >= list.mtime` invariant.
