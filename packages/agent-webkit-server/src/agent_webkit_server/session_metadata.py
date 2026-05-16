"""Persistent metadata for sessions, keyed by the wrapper UUID.

This is what makes session resume possible across process restarts. When the
in-memory ``SessionRegistry`` loses a session — uvicorn restart, server
deploy, idle reap — the metadata store still holds the wrapper UUID's
mapping to the underlying SDK session id (and the original config). On the
next lookup, the registry rebuilds a fresh in-memory ``Session`` backed by
``ClaudeSDKClient(options=ClaudeAgentOptions(resume=sdk_session_id))``,
so the agent picks up its prior transcript transparently.

The default implementation is a directory of JSON files. A Postgres or
other adapter can be plugged in by implementing the protocol.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass
class SessionMetadata:
    """Everything the registry needs to rebuild a Session from scratch."""
    id: str
    sdk_session_id: Optional[str] = None
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    cwd: Optional[str] = None
    include_partial_messages: bool = False
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SessionMetadata":
        return cls(
            id=data["id"],
            sdk_session_id=data.get("sdk_session_id"),
            model=data.get("model"),
            permission_mode=data.get("permission_mode"),
            cwd=data.get("cwd"),
            include_partial_messages=bool(data.get("include_partial_messages", False)),
            created_at=float(data.get("created_at", time.time())),
            last_seen_at=float(data.get("last_seen_at", time.time())),
        )


class SessionMetadataStore(Protocol):
    """Async key-value store for SessionMetadata, keyed by wrapper UUID."""
    async def save(self, metadata: SessionMetadata) -> None: ...
    async def load(self, session_id: str) -> Optional[SessionMetadata]: ...
    async def delete(self, session_id: str) -> None: ...
    async def list(self) -> list[SessionMetadata]: ...


class FileSessionMetadataStore:
    """One JSON file per session under ``<directory>/<uuid>.json``.

    Writes are atomic (tmp + ``os.replace``). The directory is created on
    construction if it doesn't exist. All disk I/O is offloaded to a thread
    so it can't block the event loop under load.

    Trade-offs:
      • Single-process only — concurrent processes writing the same id race
        on the rename. Fine for single-instance dev/prod; use a Postgres
        adapter for multi-instance.
      • No TTL. Metadata files accumulate; explicit ``DELETE /sessions/{id}``
        removes them. A separate cleanup loop is the right place for
        garbage collection (out of scope here).
    """

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        # Reject path-traversal attempts up front: session ids are UUIDs, so
        # any non-UUID input is malformed and shouldn't touch the filesystem.
        try:
            uuid.UUID(session_id)
        except ValueError as e:
            raise ValueError(f"invalid session id: {session_id!r}") from e
        return self._dir / f"{session_id}.json"

    async def save(self, metadata: SessionMetadata) -> None:
        path = self._path(metadata.id)
        payload = json.dumps(metadata.to_dict(), separators=(",", ":"))
        await asyncio.to_thread(self._write_atomic, path, payload)

    @staticmethod
    def _write_atomic(path: Path, payload: str) -> None:
        # Per-write unique tmp name so two concurrent writers for the same id
        # don't race on the same temp file (one's rename would leave the other
        # holding a path that's already been moved).
        tmp = path.with_suffix(path.suffix + f".tmp.{uuid.uuid4().hex}")
        try:
            tmp.write_text(payload)
            os.replace(tmp, path)
        except Exception:
            # Best-effort cleanup if the rename failed mid-flight.
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            raise

    async def load(self, session_id: str) -> Optional[SessionMetadata]:
        try:
            path = self._path(session_id)
        except ValueError:
            return None
        try:
            raw = await asyncio.to_thread(path.read_text)
        except FileNotFoundError:
            return None
        try:
            return SessionMetadata.from_dict(json.loads(raw))
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Corrupt session metadata for %s: %s", session_id, e)
            return None

    async def delete(self, session_id: str) -> None:
        try:
            path = self._path(session_id)
        except ValueError:
            return
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            pass

    async def list(self) -> list[SessionMetadata]:
        """Enumerate every persisted session. Files that fail to parse are
        skipped (logged); a single corrupt entry must not block the listing."""
        entries = await asyncio.to_thread(self._scan_dir)
        out: list[SessionMetadata] = []
        for path in entries:
            try:
                raw = await asyncio.to_thread(path.read_text)
                out.append(SessionMetadata.from_dict(json.loads(raw)))
            except (json.JSONDecodeError, KeyError, ValueError, FileNotFoundError) as e:
                logger.warning("Skipping unreadable session metadata %s: %s", path.name, e)
        return out

    def _scan_dir(self) -> list[Path]:
        # Filter to *.json (skip tmp files from in-flight writes).
        return [p for p in self._dir.iterdir() if p.suffix == ".json" and p.is_file()]
