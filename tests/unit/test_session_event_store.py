"""FileSessionEventStore — append/load round-trip + load_recent semantics."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from agent_webkit_server.event_log import LoggedEvent
from agent_webkit_server.session_event_store import FileSessionEventStore


@pytest.mark.asyncio
async def test_append_then_load_recent_roundtrips(tmp_path) -> None:
    store = FileSessionEventStore(tmp_path)
    sid = str(uuid.uuid4())
    await store.append(sid, LoggedEvent(seq=1, event="session_ready", data={"x": 1}))
    await store.append(sid, LoggedEvent(seq=2, event="message_complete", data={"text": "hi"}))

    loaded = await store.load_recent(sid)
    assert [(e.seq, e.event) for e in loaded] == [(1, "session_ready"), (2, "message_complete")]
    assert loaded[0].data == {"x": 1}


@pytest.mark.asyncio
async def test_load_recent_limit_keeps_tail(tmp_path) -> None:
    store = FileSessionEventStore(tmp_path)
    sid = str(uuid.uuid4())
    for i in range(1, 11):
        await store.append(sid, LoggedEvent(seq=i, event="x", data=i))
    # await flushes
    loaded = await store.load_recent(sid, limit=3)
    assert [e.seq for e in loaded] == [8, 9, 10]


@pytest.mark.asyncio
async def test_load_recent_returns_empty_for_unknown_session(tmp_path) -> None:
    store = FileSessionEventStore(tmp_path)
    assert await store.load_recent(str(uuid.uuid4())) == []


@pytest.mark.asyncio
async def test_delete_removes_event_file(tmp_path) -> None:
    store = FileSessionEventStore(tmp_path)
    sid = str(uuid.uuid4())
    await store.append(sid, LoggedEvent(seq=1, event="x", data=None))
    await store.load_recent(sid)  # drain pending
    await store.delete(sid)
    assert await store.load_recent(sid) == []


@pytest.mark.asyncio
async def test_appends_are_coalesced_and_durable_after_shutdown(tmp_path) -> None:
    """Rapid bursts of appends must all land — the background flusher coalesces
    but doesn't drop. shutdown() waits for the queue to drain."""
    store = FileSessionEventStore(tmp_path)
    sid = str(uuid.uuid4())
    await asyncio.gather(*(
        store.append(sid, LoggedEvent(seq=i, event="x", data=i)) for i in range(1, 51)
    ))
    await store.shutdown()
    loaded = await store.load_recent(sid, limit=100)
    assert [e.seq for e in loaded] == list(range(1, 51))


@pytest.mark.asyncio
async def test_invalid_session_id_is_silent_noop(tmp_path) -> None:
    store = FileSessionEventStore(tmp_path)
    # Bad ids must not raise; consumers shouldn't have to guard.
    await store.append("../escaped", LoggedEvent(seq=1, event="x", data=None))
    assert await store.load_recent("../escaped") == []
    await store.delete("../escaped")
