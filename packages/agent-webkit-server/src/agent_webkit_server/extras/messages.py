"""Read-through translator: SDK SessionStore transcript → wire-friendly messages.

The :class:`Session` keeps an in-memory ``messages`` buffer populated live from
``message_complete`` events and ``submit_user_message`` calls. After a process
restart that buffer is empty until the SDK replays from its store, but the SDK's
:class:`SessionStore` already holds the durable transcript. This module bridges
the two: given a SessionStore and a session id, project the persisted transcript
into the same shape the live buffer carries.

The wire-friendly shape is intentionally minimal — what an L2 reducer would
produce from the wire stream — so :func:`project_sdk_entries_to_messages`
filters out non-message entries (titles, mode markers, hook entries) and
returns only ``user``/``assistant`` rows.
"""
from __future__ import annotations

from typing import Any, Awaitable, Optional, Protocol


__all__ = [
    "SessionStoreLike",
    "project_sdk_entries_to_messages",
    "load_messages_for_session",
]


class SessionStoreLike(Protocol):
    """The single SessionStore method we lean on. Anything with ``load(key)``
    that returns SDK transcript entries works — :class:`PgSessionStore` and
    the SDK's bundled in-memory store both satisfy this."""

    async def load(self, key: dict[str, Any]) -> Optional[list[dict[str, Any]]]: ...


def project_sdk_entries_to_messages(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project SDK transcript rows into the wire-friendly buffer shape.

    Each user entry yields ``{"role": "user", "content": ...}`` and each
    assistant entry yields the inner ``message`` dict (``role``, ``content``,
    ``model``, ``stop_reason`` per the wire schema). Everything else (titles,
    mode markers, system hook entries, subagent rows the caller didn't ask
    for) is dropped — the L2 reducer ignores them too, so we don't surface
    them in the rehydration payload either.
    """
    out: list[dict[str, Any]] = []
    for e in entries:
        kind = e.get("type")
        if kind == "user":
            msg = e.get("message")
            if isinstance(msg, dict):
                content = msg.get("content")
                if content is not None:
                    out.append({"role": "user", "content": content})
        elif kind == "assistant":
            msg = e.get("message")
            if isinstance(msg, dict):
                out.append(msg)
        # Anything else is intentionally ignored — see module docstring.
    return out


async def load_messages_for_session(
    *,
    session_store: SessionStoreLike,
    project_key: str,
    session_id: str,
) -> list[dict[str, Any]]:
    """Read-through helper for ``GET /messages`` and ``GET /snapshot``.

    Returns ``[]`` when the store has no record of this session — callers
    treat that as "session unknown" and decide whether to 404 based on
    other signals (e.g. whether artefacts exist for this id)."""
    key = {"project_key": project_key, "session_id": session_id, "subpath": ""}
    entries = await session_store.load(key)
    if not entries:
        return []
    return project_sdk_entries_to_messages(entries)
