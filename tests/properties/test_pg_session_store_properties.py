"""Property-based tests for the :class:`PgSessionStore` adapter.

These run against a live Postgres (skip when ``PG_DSN`` / ``PG_USE_DEFAULT``
unset). Each property uses unique ``(project_key, session_id)`` per example
so the function-scoped fixture can be reused across Hypothesis iterations
without cross-example contamination — no per-example truncation needed.

Covers two adapter invariants worth proving by enumeration:

* **uuid idempotency under any duplication pattern**: appending the same
  uuid-bearing entries any number of times yields exactly one row per uuid.
* **project_key isolation**: writes under one project never leak into
  another's ``load`` / ``list_sessions``, regardless of how the writes are
  interleaved.
"""
from __future__ import annotations

import string
import uuid as uuidlib

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st


pytestmark = [pytest.mark.asyncio]


_id_text = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=12)


def _entry(uuid_val: str | None, payload: int) -> dict:
    e: dict = {"type": "user", "data": {"i": payload}}
    if uuid_val is not None:
        e["uuid"] = uuid_val
    return e


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    uuids=st.lists(_id_text, min_size=1, max_size=4, unique=True),
    duplications=st.lists(st.integers(min_value=1, max_value=3), min_size=1, max_size=4),
)
async def test_uuid_idempotent_under_arbitrary_duplication(fresh_pg_store, uuids, duplications) -> None:
    """For any set of uuid-bearing entries appended in any duplication pattern,
    exactly one row per uuid survives — and the surviving order matches first-write order."""
    store = fresh_pg_store
    project_key = f"proj-{uuidlib.uuid4().hex[:10]}"
    session_id = f"sess-{uuidlib.uuid4().hex[:10]}"
    key = {"project_key": project_key, "session_id": session_id}

    # Append each uuid `dup` times across one or more batches.
    for i, u in enumerate(uuids):
        dup = duplications[i % len(duplications)]
        for _ in range(dup):
            await store.append(key, [_entry(u, i)])

    loaded = await store.load(key)
    assert loaded is not None
    survived_uuids = [e["uuid"] for e in loaded]
    assert survived_uuids == uuids, (survived_uuids, uuids)


@settings(
    max_examples=15,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    project_a_writes=st.integers(min_value=1, max_value=4),
    project_b_writes=st.integers(min_value=1, max_value=4),
)
async def test_project_key_isolation_for_any_interleaving(
    fresh_pg_store, project_a_writes, project_b_writes
) -> None:
    """No write under project A is ever visible under project B (or vice versa).

    Both projects share a session_id deliberately to prove the project_key
    boundary — not session_id uniqueness — is what isolates them.
    """
    store = fresh_pg_store
    shared_session = f"shared-{uuidlib.uuid4().hex[:8]}"
    pa = f"A-{uuidlib.uuid4().hex[:8]}"
    pb = f"B-{uuidlib.uuid4().hex[:8]}"
    key_a = {"project_key": pa, "session_id": shared_session}
    key_b = {"project_key": pb, "session_id": shared_session}

    # Interleave writes A, B, A, B, ... up to the requested counts.
    a_done = b_done = 0
    while a_done < project_a_writes or b_done < project_b_writes:
        if a_done < project_a_writes:
            await store.append(key_a, [_entry(f"a-{a_done}", a_done)])
            a_done += 1
        if b_done < project_b_writes:
            await store.append(key_b, [_entry(f"b-{b_done}", b_done)])
            b_done += 1

    loaded_a = await store.load(key_a) or []
    loaded_b = await store.load(key_b) or []

    assert len(loaded_a) == project_a_writes
    assert len(loaded_b) == project_b_writes
    assert all(e["uuid"].startswith("a-") for e in loaded_a)
    assert all(e["uuid"].startswith("b-") for e in loaded_b)

    sessions_a = {s["session_id"] for s in await store.list_sessions(pa)}
    sessions_b = {s["session_id"] for s in await store.list_sessions(pb)}
    assert sessions_a == {shared_session}
    assert sessions_b == {shared_session}
