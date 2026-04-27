"""Test config — wires the agent-webkit-server source dir into sys.path so tests
import the package without requiring an editable install.

Postgres-backed fixtures live here too so both ``tests/integration`` and
``tests/properties`` can share them. They skip when ``PG_DSN`` (or
``PG_USE_DEFAULT=1``) is unset.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "agent-webkit-server" / "src"))
sys.path.insert(0, str(ROOT))


DEFAULT_DSN = "postgresql://agentwebkit:agentwebkit@127.0.0.1:55432/agentwebkit_test"


def _pg_dsn() -> str | None:
    dsn = os.environ.get("PG_DSN")
    if dsn:
        return dsn
    if os.environ.get("PG_USE_DEFAULT") == "1":
        return DEFAULT_DSN
    return None


@pytest.fixture(scope="session")
def pg_dsn() -> str:
    dsn = _pg_dsn()
    if not dsn:
        pytest.skip(
            "Postgres tests require PG_DSN (or PG_USE_DEFAULT=1 with "
            "docker compose -f docker-compose.test.yml up -d)"
        )
    return dsn


@pytest_asyncio.fixture
async def fresh_pg_store(pg_dsn: str) -> AsyncIterator:
    """A PgSessionStore connected to a freshly-truncated test database.

    Truncates between tests rather than rebuilding the schema for speed.
    """
    from agent_webkit_server.adapters.pg_session_store import PgSessionStore

    store = await PgSessionStore.connect(pg_dsn, min_size=1, max_size=4)
    async with store._pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute("TRUNCATE session_entries, session_summaries")
    try:
        yield store
    finally:
        await store.close()
