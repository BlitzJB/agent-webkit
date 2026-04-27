"""Property-based tests for :class:`EventLog`.

The event log underpins SSE delivery — its ordering and eviction invariants
are load-bearing for clients that depend on `Last-Event-ID` resume. These
properties cover behaviors example tests can't enumerate by hand:

* For any append sequence and any `after_seq` cursor, `subscribe()` yields
  exactly the suffix of events with `seq > after_seq`, in order.
* Eviction is monotonic: once `len(events) > max_size`, the oldest seq is
  no longer reachable; subscribing to an evicted seq raises ``EvictedError``.
* Subscribers are independent: their cursors don't interact.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from agent_webkit_server.event_log import EventLog, EvictedError


_event_names = st.sampled_from(["a", "b", "c", "session_ready", "result"])
_payloads = st.dictionaries(st.text(min_size=0, max_size=8), st.integers(), max_size=4)


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(events=st.lists(st.tuples(_event_names, _payloads), min_size=0, max_size=50),
       after_seq=st.integers(min_value=0, max_value=60))
@pytest.mark.asyncio
async def test_subscribe_yields_exactly_events_after_cursor(events, after_seq) -> None:
    """For any append sequence + cursor, subscribe sees `[after_seq+1 .. last]` in order."""
    log = EventLog(max_size=1000)  # large enough to avoid eviction
    appended = [log.append(name, data) for name, data in events]

    expected = [e for e in appended if e.seq > after_seq]

    # close() lets the subscribe iterator return cleanly once the suffix is drained.
    log.close()
    seen: list[Any] = []
    async for ev in log.subscribe(after_seq=after_seq):
        seen.append(ev)

    assert [(e.seq, e.event) for e in seen] == [(e.seq, e.event) for e in expected]


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    max_size=st.integers(min_value=2, max_value=8),
    n_events=st.integers(min_value=0, max_value=30),
)
@pytest.mark.asyncio
async def test_evicted_cursor_raises_for_any_overflow(max_size, n_events) -> None:
    """Once the ring overflows, the seq just before the new oldest is unreachable.

    Specifically: if we appended `n_events` and `n_events > max_size`, the seq
    `oldest - 1` (i.e. seq=1 on first overflow) must raise EvictedError.
    """
    log = EventLog(max_size=max_size)
    for i in range(n_events):
        log.append("e", i)

    if n_events > max_size:
        # The seq before the current oldest must be evicted.
        oldest = log._buf[0].seq  # type: ignore[attr-defined]
        evicted_target = oldest - 2  # safely older than oldest
        if evicted_target >= 1:
            with pytest.raises(EvictedError):
                # subscribe is async; surface the raise without consuming.
                gen = log.subscribe(after_seq=evicted_target).__aiter__()
                await gen.__anext__()


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    events=st.lists(st.tuples(_event_names, _payloads), min_size=0, max_size=20),
    cursors=st.lists(st.integers(min_value=0, max_value=25), min_size=2, max_size=5),
)
@pytest.mark.asyncio
async def test_multi_subscriber_views_are_independent(events, cursors) -> None:
    """N subscribers with N different cursors each see exactly their own suffix."""
    log = EventLog(max_size=1000)
    appended = [log.append(name, data) for name, data in events]
    log.close()

    async def collect(c: int) -> list[int]:
        seen: list[int] = []
        async for ev in log.subscribe(after_seq=c):
            seen.append(ev.seq)
        return seen

    results = await asyncio.gather(*(collect(c) for c in cursors))
    for c, got in zip(cursors, results):
        expected = [e.seq for e in appended if e.seq > c]
        assert got == expected, (c, got, expected)
