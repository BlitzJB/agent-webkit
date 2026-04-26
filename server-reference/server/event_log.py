"""Per-session append-only event log with multi-subscriber fan-out.

Each event has a server-assigned monotonic seq id. The log is a bounded ring (default 1000
events). Subscribers tail with their own cursor; if a subscriber requests a seq that has
been evicted, the server raises EvictedError → 412 Precondition Failed.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Any, AsyncIterator


@dataclass
class LoggedEvent:
    seq: int
    event: str
    data: Any


class EvictedError(Exception):
    """Requested Last-Event-ID is older than the oldest event still in the ring."""


class EventLog:
    def __init__(self, max_size: int = 1000) -> None:
        self._max = max_size
        self._buf: deque[LoggedEvent] = deque(maxlen=max_size)
        self._next_seq = 1
        self._waiters: list[asyncio.Event] = []
        self._closed = False

    @property
    def last_seq(self) -> int:
        return self._next_seq - 1

    def append(self, event: str, data: Any) -> LoggedEvent:
        if self._closed:
            raise RuntimeError("Event log is closed")
        ev = LoggedEvent(seq=self._next_seq, event=event, data=data)
        self._next_seq += 1
        self._buf.append(ev)
        # Wake all waiters; they'll re-check their cursor.
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

    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[LoggedEvent]:
        """Yield events with seq > after_seq, blocking when caught up.
        Raises EvictedError if after_seq is older than the ring.
        """
        if after_seq > 0 and self._buf and after_seq < self._oldest_seq() - 1:
            raise EvictedError(
                f"Last-Event-ID {after_seq} evicted; oldest available is {self._oldest_seq()}"
            )

        cursor = after_seq
        while True:
            # Drain everything past cursor.
            for ev in list(self._buf):
                if ev.seq > cursor:
                    cursor = ev.seq
                    yield ev
            if self._closed:
                return
            # Wait for a new append.
            waiter = asyncio.Event()
            self._waiters.append(waiter)
            try:
                await waiter.wait()
            finally:
                pass
