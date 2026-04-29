"""Integration tests for :class:`PgArtefactStore`.

Validates contracts that an in-memory test cannot exercise: real Postgres
advisory-lock serialisation under concurrency, soft-delete semantics across
pool acquisitions, and cross-session row isolation.

Skipped when no Postgres DSN is configured — see ``conftest.py``.
"""
from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio

from agent_webkit_server.adapters.pg_artefact_store import PgArtefactStore
from agent_webkit_server.extras.artefacts import ArtefactNotFoundError


@pytest_asyncio.fixture
async def fresh_pg_artefact_store(pg_dsn: str):
    """A PgArtefactStore connected to a freshly-truncated test database."""
    store = await PgArtefactStore.connect(pg_dsn, min_size=1, max_size=8)
    async with store._pool.acquire() as conn:  # type: ignore[attr-defined]
        await conn.execute("TRUNCATE artefact_versions, artefacts")
    try:
        yield store
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_create_then_read_round_trips(fresh_pg_artefact_store: PgArtefactStore) -> None:
    art, ver = await fresh_pg_artefact_store.create(
        session_id="s1", title="hello", kind="text/plain", content="body"
    )
    assert art.current_version == 1
    assert ver.version == 1
    art2, ver2 = await fresh_pg_artefact_store.read(artefact_id=art.id)
    assert art2.id == art.id
    assert ver2.content == "body"


@pytest.mark.asyncio
async def test_concurrent_updates_serialise_to_monotonic_versions(
    fresh_pg_artefact_store: PgArtefactStore,
) -> None:
    """20 concurrent updates against one artefact must produce versions
    1..21 with no gaps, no duplicates, and no lost writes.

    Validates the per-artefact ``pg_advisory_xact_lock`` actually serialises
    the read-modify-write of ``current_version``.

    (Note: ``update()`` returns the latest read, not the version it wrote,
    so we assert via ``list_versions`` which reflects every persisted row.)
    """
    art, _ = await fresh_pg_artefact_store.create(
        session_id="s", title="t", kind="text/plain", content="v1"
    )

    async def bump(i: int) -> None:
        await fresh_pg_artefact_store.update(artefact_id=art.id, content=f"c{i}")

    await asyncio.gather(*(bump(i) for i in range(2, 22)))

    final = await fresh_pg_artefact_store.get(artefact_id=art.id)
    assert final.current_version == 21
    all_versions = await fresh_pg_artefact_store.list_versions(artefact_id=art.id)
    assert [v.version for v in all_versions] == list(range(1, 22))
    # No duplicates — primary key would have rejected, but assert directly.
    assert len({v.version for v in all_versions}) == 21


@pytest.mark.asyncio
async def test_soft_delete_hides_artefact_across_pool_acquisitions(
    fresh_pg_artefact_store: PgArtefactStore,
) -> None:
    """After delete: get/read/update raise NotFound; list_for_session excludes it.

    Verifies the partial index + ``deleted_at_ms IS NULL`` predicate hold
    regardless of which pool connection answers the read.
    """
    art, _ = await fresh_pg_artefact_store.create(
        session_id="s", title="t", kind="text/plain", content="v"
    )
    await fresh_pg_artefact_store.delete(artefact_id=art.id)

    with pytest.raises(ArtefactNotFoundError):
        await fresh_pg_artefact_store.get(artefact_id=art.id)
    with pytest.raises(ArtefactNotFoundError):
        await fresh_pg_artefact_store.read(artefact_id=art.id)
    with pytest.raises(ArtefactNotFoundError):
        await fresh_pg_artefact_store.update(artefact_id=art.id, content="x")
    with pytest.raises(ArtefactNotFoundError):
        await fresh_pg_artefact_store.delete(artefact_id=art.id)

    # Hammer the partial-index path across many connections in parallel.
    results = await asyncio.gather(
        *(fresh_pg_artefact_store.list_for_session(session_id="s") for _ in range(8))
    )
    for rows in results:
        assert rows == []


@pytest.mark.asyncio
async def test_list_for_session_isolates_across_sessions(
    fresh_pg_artefact_store: PgArtefactStore,
) -> None:
    """Rows from session A must never appear under session B's listing,
    even under concurrent creates."""
    async def make_for(session_id: str, n: int) -> None:
        for i in range(n):
            await fresh_pg_artefact_store.create(
                session_id=session_id, title=f"{session_id}-{i}",
                kind="text/plain", content=f"c{i}",
            )

    await asyncio.gather(make_for("A", 5), make_for("B", 7), make_for("C", 3))

    a = await fresh_pg_artefact_store.list_for_session(session_id="A")
    b = await fresh_pg_artefact_store.list_for_session(session_id="B")
    c = await fresh_pg_artefact_store.list_for_session(session_id="C")
    assert len(a) == 5 and all(r.session_id == "A" for r in a)
    assert len(b) == 7 and all(r.session_id == "B" for r in b)
    assert len(c) == 3 and all(r.session_id == "C" for r in c)


@pytest.mark.asyncio
async def test_cross_instance_persistence_survives_reconnect(pg_dsn: str) -> None:
    """A second adapter pointed at the same DSN sees prior writes — the
    cold-start replay path REST endpoints rely on after a server reboot.
    """
    bootstrap = await PgArtefactStore.connect(pg_dsn)
    try:
        async with bootstrap._pool.acquire() as conn:  # type: ignore[attr-defined]
            await conn.execute("TRUNCATE artefact_versions, artefacts")
        art, _ = await bootstrap.create(
            session_id="persist", title="t", kind="text/plain", content="orig"
        )
        await bootstrap.update(artefact_id=art.id, content="rev2")
    finally:
        await bootstrap.close()

    reader = await PgArtefactStore.connect(pg_dsn)
    try:
        rows = await reader.list_for_session(session_id="persist")
        assert len(rows) == 1
        assert rows[0].id == art.id
        assert rows[0].current_version == 2
        _, latest = await reader.read(artefact_id=art.id)
        assert latest.content == "rev2"
        v1 = await reader.read(artefact_id=art.id, version=1)
        assert v1[1].content == "orig"
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_read_specific_version_after_concurrent_updates(
    fresh_pg_artefact_store: PgArtefactStore,
) -> None:
    """Every historical version row written under concurrency is independently
    readable — no version row is lost or overwritten."""
    art, _ = await fresh_pg_artefact_store.create(
        session_id="s", title="t", kind="text/plain", content="v1"
    )

    async def bump(i: int) -> None:
        await fresh_pg_artefact_store.update(artefact_id=art.id, content=f"c{i}")

    await asyncio.gather(*(bump(i) for i in range(10)))

    versions = await fresh_pg_artefact_store.list_versions(artefact_id=art.id)
    assert len(versions) == 11
    contents = {v.content for v in versions}
    assert "v1" in contents
    assert {f"c{i}" for i in range(10)} <= contents
