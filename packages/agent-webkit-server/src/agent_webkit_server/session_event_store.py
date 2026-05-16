"""Durable per-session wire event log.

Without this, sessions resumed across a process restart attach to a fresh
EventLog with only ``session_ready`` — clients reconnecting see no
history even though the SDK has the full conversation in its transcript.

The store is intentionally separate from :class:`SessionMetadataStore`:
metadata is small and overwritten on every change, events are large and
append-only. Different access patterns, different durability requirements.

File implementation is one ``<uuid>.events.jsonl`` per session, append-only.
Each line: ``{"seq": int, "event": str, "data": <json>}``. On resume we
read the file and feed the last ``limit`` events into the new
:class:`EventLog`'s seed so the ring buffer is hot before any subscriber
attaches.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Optional, Protocol

from .event_log import LoggedEvent

logger = logging.getLogger(__name__)


class SessionEventStore(Protocol):
    """Per-session append-only durable event log."""
    async def append(self, session_id: str, event: LoggedEvent) -> None: ...
    async def load_recent(self, session_id: str, limit: int = 1000) -> list[LoggedEvent]: ...
    async def delete(self, session_id: str) -> None: ...


class FileSessionEventStore:
    """JSONL append-only file per session under ``<directory>/<uuid>.events.jsonl``.

    Writes are best-effort with an in-memory queue per session — appends from
    the event hot path return immediately; the actual fsync happens on a
    background flusher. A crash between append and fsync can lose the last
    handful of events for that session; that's the right trade for not
    blocking the SSE generator on disk I/O.
    """

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        # Per-session pending queues + flusher tasks.
        self._queues: dict[str, deque[LoggedEvent]] = {}
        self._flushers: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    def _path(self, session_id: str) -> Path:
        try:
            uuid.UUID(session_id)
        except ValueError as e:
            raise ValueError(f"invalid session id: {session_id!r}") from e
        return self._dir / f"{session_id}.events.jsonl"

    async def append(self, session_id: str, event: LoggedEvent) -> None:
        try:
            path = self._path(session_id)
        except ValueError:
            return
        # Enqueue + ensure a flusher is running. We don't await disk I/O —
        # streaming hot path must stay non-blocking.
        async with self._lock:
            q = self._queues.setdefault(session_id, deque())
            q.append(event)
            existing = self._flushers.get(session_id)
            if existing is None or existing.done():
                self._flushers[session_id] = asyncio.create_task(
                    self._flush_loop(session_id, path)
                )

    async def _flush_loop(self, session_id: str, path: Path) -> None:
        # Coalesce up to ~50ms of appends per write to keep the open()/close()
        # rate sane under bursty traffic (a single turn can emit dozens of
        # message_delta events in quick succession).
        while True:
            await asyncio.sleep(0.05)
            async with self._lock:
                q = self._queues.get(session_id)
                if not q:
                    self._flushers.pop(session_id, None)
                    return
                batch = list(q)
                q.clear()
            payload = "".join(
                json.dumps({"seq": e.seq, "event": e.event, "data": e.data}) + "\n"
                for e in batch
            )
            try:
                await asyncio.to_thread(self._append_blob, path, payload)
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "Failed to persist event batch for session %s (%d events lost)",
                    session_id,
                    len(batch),
                )

    @staticmethod
    def _append_blob(path: Path, payload: str) -> None:
        # Open in append mode; POSIX guarantees atomic append for small writes
        # (<=PIPE_BUF). Our batches can exceed that, but a single process is
        # the only writer per session id so no interleaving concerns.
        with path.open("a", encoding="utf-8") as f:
            f.write(payload)

    async def load_recent(self, session_id: str, limit: int = 1000) -> list[LoggedEvent]:
        # Drain any pending writes for this session first so the read sees
        # everything that's been "appended" from the caller's perspective.
        await self._drain_pending(session_id)
        try:
            path = self._path(session_id)
        except ValueError:
            return []
        try:
            raw = await asyncio.to_thread(path.read_text)
        except FileNotFoundError:
            return []
        out: list[LoggedEvent] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                out.append(LoggedEvent(seq=int(obj["seq"]), event=str(obj["event"]), data=obj.get("data")))
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                logger.warning(
                    "Skipping unreadable event line in %s: %s", path.name, e
                )
        if limit and len(out) > limit:
            out = out[-limit:]
        return out

    async def _drain_pending(self, session_id: str) -> None:
        # Wait briefly for the flusher to catch up; cheaper than holding the
        # whole event log read behind a strict synchronization barrier.
        for _ in range(20):  # up to ~200ms
            async with self._lock:
                q = self._queues.get(session_id)
                if not q:
                    return
            await asyncio.sleep(0.01)

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._queues.pop(session_id, None)
            task = self._flushers.pop(session_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            path = self._path(session_id)
        except ValueError:
            return
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            pass

    async def shutdown(self) -> None:
        """Flush any pending appends. Call from app shutdown so a clean
        uvicorn stop preserves the very last events."""
        async with self._lock:
            session_ids = list(self._flushers.keys())
        for sid in session_ids:
            await self._drain_pending(sid)
