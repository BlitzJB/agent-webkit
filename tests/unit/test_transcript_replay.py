"""SDK-transcript → wire-event translation, then E2E across a restart.

The SDK is the source of truth for transcripts (they live at
``~/.claude/projects/<cwd-hash>/<session-id>.jsonl``). On resume we just
read them and translate back into wire events to seed the in-memory ring —
no duplicate journal of our own.

These tests stub ``claude_agent_sdk.get_session_messages`` to return canned
``SessionMessage`` lists so they run without real credentials or fixtures
on disk. A separate live-SDK E2E (gated on creds) covers the real reader.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig
from agent_webkit_server.session import SessionConfig
from agent_webkit_server.session_metadata import FileSessionMetadataStore, SessionMetadata
from agent_webkit_server.transcript_replay import transcript_to_events
from tests.fake_claude_sdk import FakeClaudeSDKClient
from tests.unit.test_http_app import UvicornTestServer, _free_port, _read_sse_events


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


class _StubSessionMessage:
    def __init__(self, type: str, message: dict, uuid: str = "u"):
        self.type = type
        self.message = message
        self.uuid = uuid


# ---- translator unit tests ---------------------------------------------------


def test_translates_user_assistant_alternation(monkeypatch) -> None:
    """A simple Q→A transcript yields user_message then message_complete in order."""
    fake = [
        _StubSessionMessage("user", {"role": "user", "content": "hello"}),
        _StubSessionMessage(
            "assistant",
            {
                "id": "msg_01",
                "role": "assistant",
                "content": [{"type": "text", "text": "hi back"}],
                "model": "claude-opus-4-7",
                "stop_reason": "end_turn",
            },
        ),
    ]
    monkeypatch.setattr(
        "agent_webkit_server.transcript_replay.get_session_messages",
        lambda *_a, **_kw: fake,
        raising=False,
    )
    # Patch the import-from inside the function: easiest is to inject via the
    # SDK's namespace.
    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "get_session_messages", lambda *_a, **_kw: fake)

    events = transcript_to_events("sdk-sid", cwd="/work")
    assert [e.event for e in events] == ["user_message", "message_complete"]
    assert events[0].data == {"content": "hello"}
    assert events[1].data["message"]["content"] == [{"type": "text", "text": "hi back"}]
    assert events[1].data["message"]["id"] == "msg_01"


def test_assistant_with_tool_use_emits_followup_tool_use_event(monkeypatch) -> None:
    """Mirrors live bridge: tool_use blocks inside a complete assistant message
    also fire a discrete tool_use wire event."""
    fake = [
        _StubSessionMessage(
            "assistant",
            {
                "id": "msg_02",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "let me check"},
                    {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"path": "/x"}},
                ],
            },
        ),
    ]
    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "get_session_messages", lambda *_a, **_kw: fake)

    events = transcript_to_events("sdk-sid")
    assert [e.event for e in events] == ["message_complete", "tool_use"]
    tu = events[1].data
    assert tu["tool_use_id"] == "tu_1"
    assert tu["tool_name"] == "Read"
    assert tu["input"] == {"path": "/x"}
    assert tu["message_id"] == "msg_02"


def test_user_message_with_tool_results_splits(monkeypatch) -> None:
    """The SDK echoes tool outputs as user-role messages with tool_result blocks.
    Translation splits them into discrete tool_result wire events."""
    fake = [
        _StubSessionMessage(
            "user",
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok", "is_error": False},
                ],
            },
        ),
    ]
    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "get_session_messages", lambda *_a, **_kw: fake)

    events = transcript_to_events("sdk-sid")
    # No user_message — the user-role entry was purely a tool_result echo.
    assert [e.event for e in events] == ["tool_result"]
    assert events[0].data == {"tool_use_id": "tu_1", "output": "ok", "is_error": False}


def test_seq_starts_at_starting_seq(monkeypatch) -> None:
    fake = [
        _StubSessionMessage("user", {"role": "user", "content": "x"}),
        _StubSessionMessage("assistant", {"role": "assistant", "content": [{"type": "text", "text": "y"}]}),
    ]
    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "get_session_messages", lambda *_a, **_kw: fake)

    events = transcript_to_events("sdk-sid", starting_seq=42)
    assert [e.seq for e in events] == [42, 43]


def test_returns_empty_on_missing_transcript(monkeypatch) -> None:
    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "get_session_messages", lambda *_a, **_kw: [])
    assert transcript_to_events("sdk-sid") == []


def test_returns_empty_when_sdk_reader_raises(monkeypatch) -> None:
    """Defensive: a malformed transcript on disk must not break resume — just
    surface as "no replay available" and let the session start fresh."""
    import claude_agent_sdk
    def _boom(*_a, **_kw):
        raise OSError("permission denied")
    monkeypatch.setattr(claude_agent_sdk, "get_session_messages", _boom)
    assert transcript_to_events("sdk-sid") == []


# ---- end-to-end: cross-process resume replays via the SDK reader ------------


def _factory(captured: list[SessionConfig]):
    async def factory(config: SessionConfig, can_use_tool=None):
        captured.append(config)
        return FakeClaudeSDKClient(FIXTURES / "plain_qa.jsonl", can_use_tool=can_use_tool)
    return factory


@pytest.mark.asyncio
async def test_history_endpoint_returns_translated_transcript(tmp_path, monkeypatch) -> None:
    """Full pipeline (new multiplex model): GET /sessions/{id}/history returns
    the prior conversation translated to wire-event shape — sourced from a
    stubbed SDK reader. The /stream endpoint stays a pure observer of future
    events; past events come from history."""
    metadata_dir = tmp_path / "sessions"

    sid = "12345678-1234-1234-1234-123456789012"
    store_v1 = FileSessionMetadataStore(metadata_dir)
    await store_v1.save(SessionMetadata(
        id=sid,
        sdk_session_id="sdk-historic-1",
        cwd="/work/repo",
    ))

    historic = [
        _StubSessionMessage("user", {"role": "user", "content": "what is 2+2?"}),
        _StubSessionMessage(
            "assistant",
            {
                "id": "msg_h1",
                "role": "assistant",
                "content": [{"type": "text", "text": "4"}],
            },
        ),
    ]
    import claude_agent_sdk
    monkeypatch.setattr(claude_agent_sdk, "get_session_messages", lambda *_a, **_kw: historic)

    captured: list[SessionConfig] = []
    app = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_factory(captured),
        metadata_store=FileSessionMetadataStore(metadata_dir),
    )
    port = _free_port()
    with UvicornTestServer(app, port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as c:
            r = await c.get(f"/sessions/{sid}/history")
            assert r.status_code == 200, r.text
            body = r.json()

    kinds = [e["event"] for e in body["events"]]
    assert kinds == ["user_message", "message_complete"], kinds
    assert body["events"][0]["payload"]["content"] == "what is 2+2?"
    assert body["events"][1]["payload"]["message"]["content"] == [
        {"type": "text", "text": "4"}
    ]
    # Lazy spawn: fetching history does NOT invoke the factory.
    assert captured == []
