"""Unit tests for StreamEvent → message_delta translation in sdk_bridge.

When the Claude SDK is invoked with `include_partial_messages=True`, it emits
`StreamEvent` instances wrapping raw Anthropic streaming events (message_start,
content_block_delta, etc.). The bridge must turn these into `message_delta`
wire events so L1/L2 clients can render streaming text.
"""
from __future__ import annotations

from typing import Any

import pytest

from agent_webkit_server.sdk_bridge import translate_sdk_messages
from tests.fake_claude_sdk import StreamEvent, AssistantMessage


async def _drain(messages: list[Any]) -> list[tuple[str, dict]]:
    emitted: list[tuple[str, dict]] = []

    async def aiter():
        for m in messages:
            yield m

    def emit(event: str, data: dict) -> None:
        emitted.append((event, data))

    await translate_sdk_messages(aiter(), emit)
    return emitted


def _se(event_type: str, **extra: Any) -> StreamEvent:
    return StreamEvent(
        uuid="u",
        session_id="s",
        event={"type": event_type, **extra},
    )


@pytest.mark.asyncio
async def test_text_deltas_emit_message_delta_with_text() -> None:
    """content_block_delta with text_delta → message_delta carrying just the text."""
    messages = [
        _se("message_start", message={"id": "msg_01"}),
        _se(
            "content_block_delta",
            index=0,
            delta={"type": "text_delta", "text": "Hel"},
        ),
        _se(
            "content_block_delta",
            index=0,
            delta={"type": "text_delta", "text": "lo"},
        ),
    ]
    out = await _drain(messages)
    assert out == [
        ("message_delta", {"message_id": "msg_01", "delta": {"type": "text", "text": "Hel"}}),
        ("message_delta", {"message_id": "msg_01", "delta": {"type": "text", "text": "lo"}}),
    ]


@pytest.mark.asyncio
async def test_input_json_deltas_emit_message_delta_for_genui() -> None:
    """input_json_delta → message_delta carrying the raw delta so the L1 GenUI
    pipeline can buffer partial JSON until it parses."""
    messages = [
        _se("message_start", message={"id": "msg_02"}),
        _se(
            "content_block_start",
            index=0,
            content_block={"type": "tool_use", "id": "tu_1", "name": "mcp__genui__render_foo"},
        ),
        _se(
            "content_block_delta",
            index=0,
            delta={"type": "input_json_delta", "partial_json": '{"a":'},
        ),
        _se(
            "content_block_delta",
            index=0,
            delta={"type": "input_json_delta", "partial_json": "1}"},
        ),
    ]
    out = await _drain(messages)
    # input_json_delta is forwarded verbatim, with tool_use_id stamped from
    # the open content_block, so GenUIStream can group partials by tool.
    assert out == [
        (
            "message_delta",
            {
                "message_id": "msg_02",
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"a":',
                    "tool_use_id": "tu_1",
                    "name": "mcp__genui__render_foo",
                },
            },
        ),
        (
            "message_delta",
            {
                "message_id": "msg_02",
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": "1}",
                    "tool_use_id": "tu_1",
                    "name": "mcp__genui__render_foo",
                },
            },
        ),
    ]


@pytest.mark.asyncio
async def test_stream_event_without_message_start_is_dropped() -> None:
    """Defensive: if a content_block_delta arrives before message_start, drop
    it — we cannot attribute the delta to a message_id."""
    messages = [
        _se("content_block_delta", index=0, delta={"type": "text_delta", "text": "x"}),
    ]
    out = await _drain(messages)
    assert out == []


@pytest.mark.asyncio
async def test_unrelated_stream_events_are_ignored() -> None:
    """ping, message_stop, content_block_stop etc. produce no wire events —
    the final AssistantMessage will arrive separately and emit message_complete."""
    messages = [
        _se("message_start", message={"id": "msg_03"}),
        _se("ping"),
        _se("content_block_stop", index=0),
        _se("message_delta", delta={"stop_reason": "end_turn"}),
        _se("message_stop"),
    ]
    out = await _drain(messages)
    assert out == []


@pytest.mark.asyncio
async def test_message_id_tracked_across_multiple_messages() -> None:
    """Two consecutive streamed assistant messages must each use their own id."""
    messages = [
        _se("message_start", message={"id": "msg_a"}),
        _se("content_block_delta", index=0, delta={"type": "text_delta", "text": "A"}),
        _se("message_stop"),
        _se("message_start", message={"id": "msg_b"}),
        _se("content_block_delta", index=0, delta={"type": "text_delta", "text": "B"}),
    ]
    out = await _drain(messages)
    assert [(e, d["message_id"]) for e, d in out] == [
        ("message_delta", "msg_a"),
        ("message_delta", "msg_b"),
    ]


@pytest.mark.asyncio
async def test_streaming_then_complete_preserves_message_id() -> None:
    """A streamed message followed by AssistantMessage(id=same) — both wire
    events must share the message_id so the L2 reducer can reconcile."""
    messages = [
        _se("message_start", message={"id": "msg_z"}),
        _se("content_block_delta", index=0, delta={"type": "text_delta", "text": "Hi"}),
        AssistantMessage(id="msg_z", content=[{"type": "text", "text": "Hi there"}]),
    ]
    out = await _drain(messages)
    delta_ids = [d["message_id"] for e, d in out if e == "message_delta"]
    complete_ids = [d["message_id"] for e, d in out if e == "message_complete"]
    assert delta_ids == ["msg_z"]
    assert complete_ids == ["msg_z"]


@pytest.mark.asyncio
async def test_streaming_then_complete_aligns_id_when_assistant_id_missing() -> None:
    """Reconciliation bug guard: when AssistantMessage.id is None (common — the
    SDK doesn't always populate it), `message_complete` must reuse the id from
    the prior `message_start` instead of fabricating a fresh `corr-N`. Otherwise
    L2 renders the streamed bubble and a second duplicate bubble side-by-side."""
    messages = [
        _se("message_start", message={"id": "msg_real"}),
        _se("content_block_delta", index=0, delta={"type": "text_delta", "text": "Hi"}),
        AssistantMessage(id=None, content=[{"type": "text", "text": "Hi there"}]),
    ]
    out = await _drain(messages)
    delta_ids = [d["message_id"] for e, d in out if e == "message_delta"]
    complete_ids = [d["message_id"] for e, d in out if e == "message_complete"]
    assert delta_ids == ["msg_real"]
    assert complete_ids == ["msg_real"], (
        "message_complete fell back to corr-N instead of reusing the streamed id; "
        "L2 will render a duplicate bubble"
    )


@pytest.mark.asyncio
async def test_assistant_message_without_prior_stream_still_uses_fallback() -> None:
    """Non-streaming path must keep working: no message_start → fallback id."""
    messages = [
        AssistantMessage(id=None, content=[{"type": "text", "text": "x"}]),
    ]
    out = await _drain(messages)
    complete_ids = [d["message_id"] for e, d in out if e == "message_complete"]
    assert len(complete_ids) == 1
    assert complete_ids[0].startswith("corr-")
