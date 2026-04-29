"""Tests for the EventLog graceful resume mode and replay_truncated synthetic event."""
from __future__ import annotations

import asyncio

import pytest

from agent_webkit_server.event_log import (
    EventLog,
    EvictedError,
    REPLAY_TRUNCATED_EVENT,
)


@pytest.mark.asyncio
async def test_strict_mode_raises_evicted_when_cursor_falls_off_ring() -> None:
    log = EventLog(max_size=3)
    for i in range(5):
        log.append("tick", {"i": i})
    # Ring now holds seqs 3..5; subscribing at 1 must raise.
    with pytest.raises(EvictedError):
        async for _ in log.subscribe(after_seq=1):
            pass


@pytest.mark.asyncio
async def test_graceful_mode_yields_synthetic_replay_truncated_then_resumes() -> None:
    log = EventLog(max_size=3)
    for i in range(5):
        log.append("tick", {"i": i})
    # Ring has seqs 3,4,5. Cursor=1 is evicted.
    log.close()  # so subscribe terminates after yielding remaining events

    seen = []
    async for ev in log.subscribe(after_seq=1, graceful=True):
        seen.append(ev)

    # First frame is the synthetic replay_truncated.
    assert seen[0].event == REPLAY_TRUNCATED_EVENT
    assert seen[0].data["requested_event_id"] == 1
    assert seen[0].data["oldest_available_id"] == 3
    assert seen[0].data["last_event_id"] == 5
    # synthetic seq sits one slot before oldest, so the client's next
    # Last-Event-ID lands them at the first real event still on the ring.
    assert seen[0].seq == 2
    # Then the real events still in the ring follow in order.
    assert [ev.event for ev in seen[1:]] == ["tick", "tick", "tick"]
    assert [ev.seq for ev in seen[1:]] == [3, 4, 5]


@pytest.mark.asyncio
async def test_graceful_mode_does_not_persist_synthetic_event_to_ring() -> None:
    log = EventLog(max_size=3)
    for i in range(5):
        log.append("tick", {"i": i})

    # Drain one frame from a graceful subscription, then verify the ring is unchanged.
    sub = log.subscribe(after_seq=1, graceful=True).__aiter__()
    first = await sub.__anext__()
    assert first.event == REPLAY_TRUNCATED_EVENT
    await sub.aclose()

    assert log.last_seq == 5
    assert log.oldest_seq == 3


@pytest.mark.asyncio
async def test_graceful_mode_with_fresh_cursor_behaves_like_strict() -> None:
    log = EventLog(max_size=10)
    log.append("a", {})
    log.append("b", {})
    log.close()
    seen = [ev async for ev in log.subscribe(after_seq=0, graceful=True)]
    # No eviction, no synthetic frame.
    assert [ev.event for ev in seen] == ["a", "b"]


def test_is_evicted_for_empty_ring_returns_false() -> None:
    log = EventLog(max_size=3)
    assert log.is_evicted(0) is False
    assert log.is_evicted(99) is False


def test_oldest_seq_property_tracks_eviction() -> None:
    log = EventLog(max_size=3)
    for i in range(5):
        log.append("tick", {"i": i})
    assert log.oldest_seq == 3
    assert log.last_seq == 5
