"""Process-global wire event log with multi-subscriber fan-out.

Every event from every session in this process flows into one ring buffer.
The wire protocol multiplexes them onto a single `GET /stream` connection,
with each frame tagged by its origin ``session_id``. Subscribers tail with
their own cursor; if a subscriber requests a seq that has been evicted from
the ring, the server raises EvictedError → 412 Precondition Failed.

This replaces the per-session EventLog model. Past-message history (i.e. what
existed before the subscriber connected) is no longer served via SSE replay
— callers fetch it from ``GET /sessions/{id}/history``, which sources it
from the SDK's on-disk transcript via ``transcript_replay``.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator, Optional


@dataclass
class LoggedEvent:
    seq: int
    session_id: str
    event: str
    data: Any


class EvictedError(Exception):
    """Requested Last-Event-ID is older than the oldest event still in the ring."""


class GlobalEventLog:
    """One ring buffer per process. Much larger than the old per-session
    EventLog (default 10k) since it carries traffic from all live sessions
    combined."""

    def __init__(self, max_size: int = 10_000) -> None:
        self._max = max_size
        self._buf: deque[LoggedEvent] = deque(maxlen=max_size)
        self._next_seq = 1
        self._waiters: list[asyncio.Event] = []
        self._closed = False

    @property
    def last_seq(self) -> int:
        return self._next_seq - 1

    def append(self, session_id: str, event: str, data: Any) -> LoggedEvent:
        if self._closed:
            raise RuntimeError("Event log is closed")
        ev = LoggedEvent(
            seq=self._next_seq, session_id=session_id, event=event, data=data
        )
        self._next_seq += 1
        self._buf.append(ev)
        for w in self._waiters:
            w.set()
        self._waiters = [w for w in self._waiters if not w.is_set()]
        return ev

    def close(self) -> None:
        self._closed = True
        for w in self._waiters:
            w.set()
        self._waiters = []

    def _oldest_seq(self) -> int:
        if not self._buf:
            return self._next_seq  # nothing yet — any seq <= last_seq is fine
        return self._buf[0].seq

    async def subscribe(
        self, after_seq: int = 0, session_ids: Optional[set[str]] = None
    ) -> AsyncIterator[LoggedEvent]:
        """Yield events with seq > after_seq, blocking when caught up.

        If ``session_ids`` is provided, only events from those sessions pass
        through — useful for per-session views and tests. ``None`` means no
        filter; all sessions stream through (the normal multiplexed mode).

        Raises :class:`EvictedError` if after_seq is older than the ring's
        current oldest entry.
        """
        if after_seq > 0 and self._buf and after_seq < self._oldest_seq() - 1:
            raise EvictedError(
                f"Last-Event-ID {after_seq} evicted; oldest available is {self._oldest_seq()}"
            )

        cursor = after_seq
        while True:
            for ev in list(self._buf):
                if ev.seq > cursor:
                    cursor = ev.seq
                    if session_ids is None or ev.session_id in session_ids:
                        yield ev
            if self._closed:
                return
            waiter = asyncio.Event()
            self._waiters.append(waiter)
            try:
                await waiter.wait()
            finally:
                pass
