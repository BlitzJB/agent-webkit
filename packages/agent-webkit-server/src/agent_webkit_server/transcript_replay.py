"""Translate the SDK's on-disk transcript into wire events for replay.

The Claude Agent SDK already persists every turn to
``~/.claude/projects/<cwd-hash>/<session-id>.jsonl``. The
``GET /sessions/{id}/history`` endpoint reads from that store and
translates the entries back into the same wire-event shapes the live
multiplexed stream emits — so on first-attach a client can fetch its
past once and then track future events over ``GET /stream``.

This keeps the SDK as the single source of truth for transcripts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class ReplayEvent:
    """One wire event materialized from the SDK transcript. Same shape as
    a :class:`ReplayEvent` minus the global-log specifics (session_id is
    implicit — every event in a transcript replay belongs to one session)."""
    seq: int
    event: str
    data: Any


def transcript_to_events(
    sdk_session_id: str,
    cwd: Optional[str] = None,
    *,
    starting_seq: int = 1,
) -> list[ReplayEvent]:
    """Read the SDK transcript for ``sdk_session_id`` and translate to wire events.

    Each ``SessionMessage`` (user/assistant) becomes:
      • user → ``user_message`` event with the prompt content
      • assistant → ``message_complete`` event with the full content blocks,
        plus a discrete ``tool_use`` event per tool call (mirroring what the
        live bridge emits in ``translate_sdk_messages``)
      • UserMessage carrying tool_result blocks → ``tool_result`` events

    Returns an empty list if the SDK isn't importable or the transcript
    can't be located; resume falls back to a transcript-less rebuild.
    """
    try:
        from claude_agent_sdk import get_session_messages  # type: ignore
    except ImportError:  # pragma: no cover - exercised only when the SDK isn't installed
        logger.debug("claude_agent_sdk not importable; transcript replay disabled")
        return []

    try:
        messages = get_session_messages(sdk_session_id, directory=cwd)
    except Exception:  # pragma: no cover - defensive: SDK transcript read failures
        logger.exception(
            "Failed to read transcript for sdk_session_id=%s; resuming without history",
            sdk_session_id,
        )
        return []

    if not messages:
        return []

    events: list[ReplayEvent] = []
    seq = starting_seq
    fallback_id_counter = 0

    def _next_seq() -> int:
        nonlocal seq
        s = seq
        seq += 1
        return s

    for m in messages:
        msg = m.message or {}
        # The SDK gives us the raw Anthropic message dict. Its `content` is
        # either a string (user shorthand) or a list of content blocks.
        content = msg.get("content") if isinstance(msg, dict) else None

        if m.type == "user":
            # User turns may carry tool_result blocks (the SDK echoes tool
            # outputs through UserMessage too) — split them off.
            tool_results: list[dict[str, Any]] = []
            user_content: Any = content
            if isinstance(content, list):
                non_tool: list[Any] = []
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        tool_results.append(blk)
                    else:
                        non_tool.append(blk)
                user_content = non_tool if non_tool else None

            if user_content:
                events.append(ReplayEvent(
                    seq=_next_seq(),
                    event="user_message",
                    data={"content": user_content},
                ))

            for blk in tool_results:
                events.append(ReplayEvent(
                    seq=_next_seq(),
                    event="tool_result",
                    data={
                        "tool_use_id": blk.get("tool_use_id", ""),
                        "output": blk.get("content"),
                        "is_error": bool(blk.get("is_error", False)),
                    },
                ))
            continue

        if m.type == "assistant":
            msg_id = (msg.get("id") if isinstance(msg, dict) else None) or m.uuid or _fallback_id(fallback_id_counter)
            fallback_id_counter += 1
            blocks = content if isinstance(content, list) else []
            events.append(ReplayEvent(
                seq=_next_seq(),
                event="message_complete",
                data={
                    "message_id": msg_id,
                    "message": {
                        "id": msg_id,
                        "role": "assistant",
                        "content": blocks,
                        "model": msg.get("model") if isinstance(msg, dict) else None,
                        "stop_reason": msg.get("stop_reason") if isinstance(msg, dict) else None,
                    },
                },
            ))
            # Mirror the live bridge: surface each tool_use as a discrete event.
            for blk in blocks:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    events.append(ReplayEvent(
                        seq=_next_seq(),
                        event="tool_use",
                        data={
                            "message_id": msg_id,
                            "tool_use_id": blk.get("id", ""),
                            "tool_name": blk.get("name", ""),
                            "input": blk.get("input", {}),
                        },
                    ))

    return events


def _fallback_id(n: int) -> str:
    return f"replay-{n}"
