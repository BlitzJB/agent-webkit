"""Unit tests for GlobalEventLog — multiplexed ring buffer + fan-out."""
import asyncio

import pytest

from agent_webkit_server.event_log import EvictedError, GlobalEventLog


async def _collect(
    log: GlobalEventLog,
    after: int = 0,
    limit: int | None = None,
    session_ids: set[str] | None = None,
) -> list[int]:
    seen: list[int] = []
    async for ev in log.subscribe(after_seq=after, session_ids=session_ids):
        seen.append(ev.seq)
        if limit and len(seen) >= limit:
            return seen
    return seen


@pytest.mark.asyncio
async def test_subscribe_yields_appended_events():
    log = GlobalEventLog()
    log.append("sid_A", "a", {})
    log.append("sid_A", "b", {})

    task = asyncio.create_task(_collect(log, after=0, limit=3))
    await asyncio.sleep(0.01)
    log.append("sid_B", "c", {})
    seen = await asyncio.wait_for(task, timeout=1.0)
    assert seen == [1, 2, 3]


@pytest.mark.asyncio
async def test_subscribe_filters_by_session_ids():
    """Per-session filtering — used internally by helpers, not by the wire
    (the wire delivers everything multiplexed)."""
    log = GlobalEventLog()
    log.append("sid_A", "a", {})
    log.append("sid_B", "b", {})
    log.append("sid_A", "c", {})

    task = asyncio.create_task(_collect(log, after=0, limit=2, session_ids={"sid_A"}))
    seen = await asyncio.wait_for(task, timeout=1.0)
    assert seen == [1, 3]


@pytest.mark.asyncio
async def test_appended_event_carries_session_id():
    log = GlobalEventLog()
    log.append("sid_X", "hello", {"k": 1})

    async for ev in log.subscribe(after_seq=0):
        assert ev.session_id == "sid_X"
        assert ev.event == "hello"
        assert ev.data == {"k": 1}
        assert ev.seq == 1
        break


@pytest.mark.asyncio
async def test_resume_from_last_event_id():
    log = GlobalEventLog()
    log.append("s", "a", {})
    log.append("s", "b", {})
    log.append("s", "c", {})

    seen = []
    async for ev in log.subscribe(after_seq=2):
        seen.append(ev.seq)
        if ev.seq == 3:
            break
    assert seen == [3]


@pytest.mark.asyncio
async def test_evicted_raises():
    log = GlobalEventLog(max_size=3)
    for _ in range(5):
        log.append("s", "x", {})
    # Oldest seq now is 3 (1 and 2 evicted). Subscribing from seq=1 should raise.
    with pytest.raises(EvictedError):
        async for _ in log.subscribe(after_seq=1):
            break


@pytest.mark.asyncio
async def test_multi_subscriber_independent_cursors():
    log = GlobalEventLog()
    log.append("s", "a", {})
    log.append("s", "b", {})

    sub1 = asyncio.create_task(_collect(log, after=0, limit=3))
    sub2 = asyncio.create_task(_collect(log, after=1, limit=2))
    await asyncio.sleep(0.01)
    log.append("s", "c", {})

    s1 = await asyncio.wait_for(sub1, timeout=1.0)
    s2 = await asyncio.wait_for(sub2, timeout=1.0)
    assert s1 == [1, 2, 3]
    assert s2 == [2, 3]


@pytest.mark.asyncio
async def test_close_terminates_subscribers():
    log = GlobalEventLog()
    log.append("s", "a", {})

    async def collect():
        out = []
        async for ev in log.subscribe(after_seq=0):
            out.append(ev.seq)
        return out

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.01)
    log.close()
    out = await asyncio.wait_for(task, timeout=1.0)
    assert out == [1]
