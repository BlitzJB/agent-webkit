"""Per-session append-only event log with multi-subscriber fan-out.

Each event has a server-assigned monotonic seq id. The log is a bounded ring
(default 1000 events). Subscribers tail with their own cursor.

Eviction has two recovery shapes; the caller picks one per subscription:

1. **Strict** (default, ``graceful=False``): if a subscriber requests an
   ``after_seq`` older than the ring's tail, raise :class:`EvictedError`.
   The HTTP layer maps this to ``412 Precondition Failed`` so the client
   knows precisely that history was lost. This is the original 1.0 behaviour
   and stays the default for wire compatibility.
2. **Graceful** (``graceful=True``, used by ``?graceful=1`` SSE clients): if
   the cursor is evicted, yield a synthetic
   ``LoggedEvent(event="replay_truncated", ...)`` as the very first frame,
   then resume from the ring's oldest available seq. Stateful clients (the L2
   ``useArtefacts`` / future ``useAgentSession`` rehydration path) react to
   this frame by hitting the REST snapshot endpoints to rebuild state.

The graceful mode is what makes :class:`ArtefactStore` and other persistent
features survive a wide reconnect gap without sounding the alarm — the SSE
log is a *cache* of recent activity, the persistent stores are the source of
truth, and the ``replay_truncated`` frame is the bridge between the two.
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


REPLAY_TRUNCATED_EVENT = "replay_truncated"


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

    @property
    def max_size(self) -> int:
        return self._max

    @property
    def oldest_seq(self) -> int:
        """Smallest seq still retained in the ring (or ``next_seq`` if empty)."""
        return self._oldest_seq()

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

    def is_evicted(self, after_seq: int) -> bool:
        """True iff a fresh subscription at ``after_seq`` would skip events."""
        if after_seq <= 0:
            return False
        if not self._buf:
            return False
        return after_seq < self._oldest_seq() - 1

    async def subscribe(
        self, after_seq: int = 0, *, graceful: bool = False
    ) -> AsyncIterator[LoggedEvent]:
        """Yield events with seq > after_seq, blocking when caught up.

        If ``after_seq`` is older than the ring tail:

        * ``graceful=False`` (default) → raise :class:`EvictedError` immediately.
        * ``graceful=True`` → yield a synthetic ``replay_truncated`` event with
          ``{requested_event_id, oldest_available_id, last_event_id}``, then
          resume from the ring's oldest seq. The synthetic event's seq is
          ``oldest_available - 1`` so the client's next ``Last-Event-ID``
          (taken from the synthetic frame) lands one slot before the first
          real event — exactly where you want to resume from after a state
          rehydrate. The synthetic event is *not* persisted to the ring.
        """
        if self.is_evicted(after_seq):
            if not graceful:
                raise EvictedError(
                    f"Last-Event-ID {after_seq} evicted; "
                    f"oldest available is {self._oldest_seq()}"
                )
            oldest = self._oldest_seq()
            synthetic_seq = oldest - 1
            yield LoggedEvent(
                seq=synthetic_seq,
                event=REPLAY_TRUNCATED_EVENT,
                data={
                    "requested_event_id": after_seq,
                    "oldest_available_id": oldest,
                    "last_event_id": self.last_seq,
                },
            )
            after_seq = synthetic_seq

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
