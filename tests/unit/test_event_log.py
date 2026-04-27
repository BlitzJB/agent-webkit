"""Unit tests for the EventLog ring buffer + multi-subscriber fan-out."""
import asyncio

import pytest

from agent_webkit_server.event_log import EventLog, EvictedError


async def _collect(log: EventLog, after: int = 0, limit: int | None = None) -> list[int]:
    seen: list[int] = []
    async for ev in log.subscribe(after_seq=after):
        seen.append(ev.seq)
        if limit and len(seen) >= limit:
            return seen
    return seen


@pytest.mark.asyncio
async def test_subscribe_yields_appended_events():
    log = EventLog()
    log.append("a", {})
    log.append("b", {})

    task = asyncio.create_task(_collect(log, after=0, limit=3))
    await asyncio.sleep(0.01)
    log.append("c", {})
    seen = await asyncio.wait_for(task, timeout=1.0)
    assert seen == [1, 2, 3]


@pytest.mark.asyncio
async def test_resume_from_last_event_id():
    log = EventLog()
    log.append("a", {})
    log.append("b", {})
    log.append("c", {})

    seen = []
    async for ev in log.subscribe(after_seq=2):
        seen.append(ev.seq)
        if ev.seq == 3:
            break
    assert seen == [3]


@pytest.mark.asyncio
async def test_evicted_raises():
    log = EventLog(max_size=3)
    for _ in range(5):
        log.append("x", {})
    # Oldest seq now is 3 (1 and 2 evicted). Subscribing from seq=1 should raise.
    with pytest.raises(EvictedError):
        async for _ in log.subscribe(after_seq=1):
            break


@pytest.mark.asyncio
async def test_multi_subscriber_independent_cursors():
    log = EventLog()
    log.append("a", {})
    log.append("b", {})

    sub1 = asyncio.create_task(_collect(log, after=0, limit=3))
    sub2 = asyncio.create_task(_collect(log, after=1, limit=2))
    await asyncio.sleep(0.01)
    log.append("c", {})

    s1 = await asyncio.wait_for(sub1, timeout=1.0)
    s2 = await asyncio.wait_for(sub2, timeout=1.0)
    assert s1 == [1, 2, 3]
    assert s2 == [2, 3]


@pytest.mark.asyncio
async def test_close_terminates_subscribers():
    log = EventLog()
    log.append("a", {})

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
