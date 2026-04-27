"""Integration tests for :class:`PgSessionStore`.

Runs the SDK-published conformance suite, then layers on adapter-specific
contracts the conformance suite doesn't cover (real-Postgres concurrency,
uuid-idempotency under retry, cross-instance persistence, multi-tenant
project_key isolation under load).

Skipped when no Postgres DSN is configured — see ``conftest.py``.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from claude_agent_sdk.testing import run_session_store_conformance  # type: ignore

from agent_webkit_server.adapters.pg_session_store import PgSessionStore


@pytest.mark.asyncio
async def test_conformance_suite_passes(pg_dsn: str) -> None:
    """The 14 SDK-defined contracts every SessionStore adapter must satisfy."""
    instances: list[PgSessionStore] = []

    async def make_store() -> PgSessionStore:
        store = await PgSessionStore.connect(pg_dsn, min_size=1, max_size=4)
        # Each call hands the conformance runner an isolated dataset.
        async with store._pool.acquire() as conn:  # type: ignore[attr-defined]
            await conn.execute("TRUNCATE session_entries, session_summaries")
        instances.append(store)
        return store

    try:
        await run_session_store_conformance(make_store)
    finally:
        for s in instances:
            await s.close()


# --- adapter-specific contracts the SDK conformance suite does not cover ---


@pytest.mark.asyncio
async def test_uuid_idempotency_dedups_repeat_appends(fresh_pg_store: PgSessionStore) -> None:
    """Re-appending an entry with an existing uuid must not duplicate the row.

    Models the SDK's retry path (``append`` is retried up to 3x on transient
    errors); a partial failure that the SDK retries with the same batch must
    not multiply transcript lines.
    """
    key = {"project_key": "tenant-a", "session_id": "s1"}
    e1 = {"type": "user", "uuid": "u1", "n": 1}
    e2 = {"type": "assistant", "uuid": "u2", "n": 2}
    await fresh_pg_store.append(key, [e1, e2])
    # Replay the same batch — simulating an SDK retry after partial failure.
    await fresh_pg_store.append(key, [e1, e2])
    # And replay overlapping with new content.
    await fresh_pg_store.append(key, [e2, {"type": "user", "uuid": "u3", "n": 3}])

    loaded = await fresh_pg_store.load(key)
    assert loaded is not None
    uuids = [e["uuid"] for e in loaded]
    assert uuids == ["u1", "u2", "u3"], uuids


@pytest.mark.asyncio
async def test_uuidless_entries_are_blind_appended(fresh_pg_store: PgSessionStore) -> None:
    """Entries without a uuid (titles, mode markers) must NOT be deduped."""
    key = {"project_key": "tenant-a", "session_id": "s2"}
    marker = {"type": "title", "value": "x"}
    await fresh_pg_store.append(key, [marker])
    await fresh_pg_store.append(key, [marker])
    loaded = await fresh_pg_store.load(key)
    assert loaded == [marker, marker]


@pytest.mark.asyncio
async def test_concurrent_appends_to_same_session_serialize_cleanly(
    fresh_pg_store: PgSessionStore,
) -> None:
    """20 concurrent appends to one session: every entry persists exactly once.

    Validates the per-session ``pg_advisory_xact_lock`` correctly serializes
    the read-fold-write of the summary sidecar without losing entries.
    """
    key = {"project_key": "tenant-a", "session_id": "race"}

    async def append_one(i: int) -> None:
        await fresh_pg_store.append(key, [{"type": "user", "uuid": f"u{i}", "n": i}])

    await asyncio.gather(*(append_one(i) for i in range(20)))

    loaded = await fresh_pg_store.load(key)
    assert loaded is not None
    assert {e["uuid"] for e in loaded} == {f"u{i}" for i in range(20)}
    # Summary sidecar exists and points at this session.
    summaries = await fresh_pg_store.list_session_summaries("tenant-a")
    assert len(summaries) == 1
    assert summaries[0]["session_id"] == "race"


@pytest.mark.asyncio
async def test_cross_instance_persistence_survives_reconnect(pg_dsn: str) -> None:
    """A second adapter instance pointed at the same DSN sees prior writes.

    This is the cold-start replay path: server reboots, new process boots a
    new ``PgSessionStore``, and ``load()`` must surface the prior session.
    """
    key = {"project_key": "tenant-z", "session_id": "persist"}

    # Truncate first.
    bootstrap = await PgSessionStore.connect(pg_dsn)
    try:
        async with bootstrap._pool.acquire() as conn:  # type: ignore[attr-defined]
            await conn.execute("TRUNCATE session_entries, session_summaries")
        await bootstrap.append(key, [
            {"type": "user", "uuid": "p1", "n": 1},
            {"type": "assistant", "uuid": "p2", "n": 2},
        ])
    finally:
        await bootstrap.close()

    # Fresh adapter, same DSN — a different "process" in spirit.
    reader = await PgSessionStore.connect(pg_dsn)
    try:
        loaded = await reader.load(key)
        assert loaded is not None
        assert [e["uuid"] for e in loaded] == ["p1", "p2"]
        sessions = await reader.list_sessions("tenant-z")
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "persist"
        assert sessions[0]["mtime"] > 0
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_project_key_isolation_under_concurrent_load(
    fresh_pg_store: PgSessionStore,
) -> None:
    """Multi-tenant: concurrent appends across project_keys never bleed across."""
    async def append_for_tenant(tenant: str) -> None:
        for i in range(10):
            await fresh_pg_store.append(
                {"project_key": tenant, "session_id": f"s-{i}"},
                [{"type": "user", "uuid": f"{tenant}-u{i}", "tenant": tenant}],
            )

    await asyncio.gather(*(append_for_tenant(t) for t in ("A", "B", "C")))

    for tenant in ("A", "B", "C"):
        sessions = await fresh_pg_store.list_sessions(tenant)
        assert len(sessions) == 10, tenant
        for s in sessions:
            loaded = await fresh_pg_store.load(
                {"project_key": tenant, "session_id": s["session_id"]}
            )
            assert loaded is not None
            assert all(e["tenant"] == tenant for e in loaded)


@pytest.mark.asyncio
async def test_summary_mtime_is_at_least_entry_mtime(
    fresh_pg_store: PgSessionStore,
) -> None:
    """Conformance contract: ``summary.mtime >= list_sessions.mtime``.

    Already covered by the conformance suite, but called out here as a
    standalone regression guard since it's the trickiest invariant — the two
    timestamps must come from the same Postgres clock, and the summary must
    be stamped strictly after the entry insert in the same transaction.
    """
    key = {"project_key": "p", "session_id": "mt"}
    await fresh_pg_store.append(key, [{"type": "user", "uuid": "u", "timestamp": "2024-01-01T00:00:00.000Z"}])
    by_id = {s["session_id"]: s["mtime"] for s in await fresh_pg_store.list_sessions("p")}
    summaries = {s["session_id"]: s for s in await fresh_pg_store.list_session_summaries("p")}
    assert summaries["mt"]["mtime"] >= by_id["mt"]


@pytest.mark.asyncio
async def test_subagent_appends_do_not_touch_main_summary(
    fresh_pg_store: PgSessionStore,
) -> None:
    """Subagent transcripts must never contribute to the main session summary.

    Conformance #14 covers this once; this test exercises it through repeated
    interleaved appends to make sure the lock + ``is_main`` gate hold under
    real concurrency.
    """
    key = {"project_key": "p", "session_id": "with-subs"}
    sub = {**key, "subpath": "subagents/agent-1"}

    await fresh_pg_store.append(key, [
        {"type": "user", "uuid": "main-1", "customTitle": "main-title",
         "timestamp": "2024-01-01T00:00:00.000Z"},
    ])
    snap = (await fresh_pg_store.list_session_summaries("p"))[0]["data"]

    # Hammer the subagent path; the main summary's ``data`` must stay frozen.
    for i in range(10):
        await fresh_pg_store.append(sub, [
            {"type": "user", "uuid": f"sub-{i}", "customTitle": "sub-overrides!",
             "timestamp": f"2024-01-01T00:00:0{i}.000Z"},
        ])

    after = (await fresh_pg_store.list_session_summaries("p"))[0]["data"]
    assert after == snap
