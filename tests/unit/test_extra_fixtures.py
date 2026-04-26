"""Coverage for the new fixtures: mcp_status_change and interrupt_mid_stream."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from server.session import SessionConfig, SessionRegistry
from tests.fake_claude_sdk import FakeClaudeSDKClient

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _factory(name: str):
    async def f(_c, can_use_tool=None):
        return FakeClaudeSDKClient(FIXTURES / f"{name}.jsonl", can_use_tool=can_use_tool)
    return f


async def _drain(log, stop_event: str, timeout: float = 2.0) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []

    async def collect() -> None:
        async for ev in log.subscribe(after_seq=0):
            out.append((ev.event, ev.data))
            if ev.event == stop_event:
                return

    await asyncio.wait_for(collect(), timeout=timeout)
    return out


@pytest.mark.asyncio
async def test_mcp_status_change_event_emitted():
    reg = SessionRegistry(_factory("mcp_status_change"))
    s = await reg.create(SessionConfig())
    try:
        await s.submit_user_message("hi")
        events = await _drain(s.event_log, "result")
        names = [e[0] for e in events]
        assert "mcp_status_change" in names
        mcp = next(d for n, d in events if n == "mcp_status_change")
        assert mcp == {"server_name": "notion", "status": "connected"}
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_interrupt_mid_stream_surfaces_interrupted_subtype():
    reg = SessionRegistry(_factory("interrupt_mid_stream"))
    s = await reg.create(SessionConfig())
    try:
        await s.submit_user_message("go")
        events = await _drain(s.event_log, "result")
        result = next(d for n, d in events if n == "result")
        assert result["subtype"] == "interrupted"
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_image_attachment_user_message_round_trips():
    """A user_message can carry an image content block; assistant replies normally."""
    reg = SessionRegistry(_factory("image_attachment"))
    s = await reg.create(SessionConfig())
    try:
        await s.submit_user_message([
            {"type": "text", "text": "what's in this image?"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}},
        ])
        events = await _drain(s.event_log, "result")
        complete = next(d for n, d in events if n == "message_complete")
        assert "red square" in complete["message"]["content"][0]["text"]
    finally:
        await reg.shutdown()


async def _drain_with_auto_allow(session) -> list[tuple[str, dict]]:
    """Drain events; auto-resolve any permission_request as allow. Stops on `result`."""
    out: list[tuple[str, dict]] = []

    async def run() -> None:
        async for ev in session.event_log.subscribe(after_seq=0):
            out.append((ev.event, ev.data))
            if ev.event == "permission_request":
                session.resolve_permission(ev.data["correlation_id"], "allow")
            if ev.event == "result":
                return

    await asyncio.wait_for(run(), timeout=5.0)
    return out


@pytest.mark.asyncio
async def test_multi_tool_emits_two_tool_use_and_two_tool_result_events():
    reg = SessionRegistry(_factory("multi_tool"))
    s = await reg.create(SessionConfig())
    try:
        await s.submit_user_message("read both")
        events = await _drain_with_auto_allow(s)
        names = [n for n, _ in events]
        assert names.count("tool_use") == 2
        assert names.count("tool_result") == 2
        ids = [d["tool_use_id"] for n, d in events if n == "tool_use"]
        assert ids == ["tu_1", "tu_2"]
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_tool_error_propagates_is_error_true():
    reg = SessionRegistry(_factory("tool_error"))
    s = await reg.create(SessionConfig())
    try:
        await s.submit_user_message("read")
        events = await _drain_with_auto_allow(s)
        tr = next(d for n, d in events if n == "tool_result")
        assert tr["is_error"] is True
        assert "ENOENT" in str(tr["output"])
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_streaming_long_message_complete_carries_all_blocks():
    """Multiple text blocks in one AssistantMessage flow through as one message_complete."""
    reg = SessionRegistry(_factory("streaming_long"))
    s = await reg.create(SessionConfig())
    try:
        await s.submit_user_message("write three parts")
        events = await _drain(s.event_log, "result", timeout=3.0)
        complete = next(d for n, d in events if n == "message_complete")
        texts = [b["text"] for b in complete["message"]["content"] if b["type"] == "text"]
        assert texts == ["part one. ", "part two. ", "part three."]
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_ask_user_question_via_session_registry_emits_event_and_resolves():
    """Drive AskUserQuestion through SessionRegistry so build_can_use_tool runs end-to-end."""
    reg = SessionRegistry(_factory("ask_user_question"))
    s = await reg.create(SessionConfig())
    try:
        await s.submit_user_message("go")
        out: list[tuple[str, dict]] = []

        async def run() -> None:
            async for ev in s.event_log.subscribe(after_seq=0):
                out.append((ev.event, ev.data))
                if ev.event == "ask_user_question":
                    s.resolve_question(
                        ev.data["correlation_id"],
                        [{"question": "Pick a color", "selectedOptions": ["red"]}],
                    )
                if ev.event == "result":
                    return

        await asyncio.wait_for(run(), timeout=5.0)
        names = [n for n, _ in out]
        assert "ask_user_question" in names
        assert names[-1] == "result"
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_deny_with_interrupt_yields_interrupted_result():
    reg = SessionRegistry(_factory("deny_with_interrupt"))
    s = await reg.create(SessionConfig())
    try:
        await s.submit_user_message("rm")
        out: list[tuple[str, dict]] = []

        async def run() -> None:
            async for ev in s.event_log.subscribe(after_seq=0):
                out.append((ev.event, ev.data))
                if ev.event == "permission_request":
                    s.resolve_permission(ev.data["correlation_id"], "deny", interrupt=True, message="nope")
                if ev.event == "result":
                    return

        await asyncio.wait_for(run(), timeout=5.0)
        result = next(d for n, d in out if n == "result")
        assert result["subtype"] == "interrupted"
    finally:
        await reg.shutdown()


@pytest.mark.asyncio
async def test_multi_turn_conversation_two_results_in_one_session():
    """`expect_user_query` appears twice — second user_message must drive the second turn."""
    reg = SessionRegistry(_factory("multi_turn"))
    s = await reg.create(SessionConfig())
    try:
        await s.submit_user_message("hi")
        await _drain(s.event_log, "result", timeout=3.0)
        # Send the second user message; expect another result.
        await s.submit_user_message("I'm Pat")
        # Drain past the first result to find the second — collect all events then count.
        all_evts: list[tuple[str, dict]] = []

        async def collect() -> None:
            async for ev in s.event_log.subscribe(after_seq=0):
                all_evts.append((ev.event, ev.data))
                if sum(1 for n, _ in all_evts if n == "result") >= 2:
                    return

        await asyncio.wait_for(collect(), timeout=3.0)
        result_count = sum(1 for n, _ in all_evts if n == "result")
        assert result_count == 2
        # And both assistant messages must be present.
        msg_ids = [d["message_id"] for n, d in all_evts if n == "message_complete"]
        assert msg_ids == ["m_1", "m_2"]
    finally:
        await reg.shutdown()
