"""Wire-event replay across process restarts.

This is the missing tier. Before this:
  • In-memory ring buffer held events; reattaching within one process saw them.
  • Cross-process resume rebuilt a fresh EventLog with just session_ready —
    any client reconnecting saw an empty transcript even though the SDK
    still had full context.

With FileSessionEventStore wired in, every wire event is mirrored to a
per-session JSONL on disk. On resume, the new EventLog is seeded from
disk so the very first attach gets the full transcript replayed.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig
from agent_webkit_server.session import SessionConfig
from agent_webkit_server.session_metadata import FileSessionMetadataStore
from agent_webkit_server.session_event_store import FileSessionEventStore
from tests.fake_claude_sdk import FakeClaudeSDKClient
from tests.unit.test_http_app import UvicornTestServer, _free_port, _read_sse_events


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _factory(captured: list[SessionConfig]):
    async def factory(config: SessionConfig, can_use_tool=None):
        captured.append(config)
        return FakeClaudeSDKClient(FIXTURES / "plain_qa.jsonl", can_use_tool=can_use_tool)
    return factory


@pytest.mark.asyncio
async def test_attach_after_restart_replays_full_transcript(tmp_path) -> None:
    """The user's reported bug: switching back to a session after a server
    restart shows no messages. With the event store wired up, the rebuilt
    session's EventLog is seeded from disk so attach replays everything."""
    metadata_dir = tmp_path / "sessions"
    metadata_store_v1 = FileSessionMetadataStore(metadata_dir)
    event_store_v1 = FileSessionEventStore(metadata_dir)
    captured_v1: list[SessionConfig] = []

    app_v1 = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_factory(captured_v1),
        metadata_store=metadata_store_v1,
        event_store=event_store_v1,
    )
    port_v1 = _free_port()
    sid: str

    with UvicornTestServer(app_v1, port_v1):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port_v1}", timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            await c.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message", "content": "what is 2+2?"},
            )
            await _read_sse_events(c, f"/sessions/{sid}/stream", stop_at="result", timeout=10.0)

    # Drain the event store's pending writes before "restarting."
    await event_store_v1.shutdown()

    # Phase 2: fresh process, fresh stores pointed at same dirs.
    event_store_v2 = FileSessionEventStore(metadata_dir)
    captured_v2: list[SessionConfig] = []
    app_v2 = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_factory(captured_v2),
        metadata_store=FileSessionMetadataStore(metadata_dir),
        event_store=event_store_v2,
    )
    port_v2 = _free_port()

    with UvicornTestServer(app_v2, port_v2):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port_v2}", timeout=10.0) as c:
            # Attach to the OLD session id. Drain past `result` again —
            # replayed events arrive in original order before any new ones.
            events = await _read_sse_events(
                c, f"/sessions/{sid}/stream", stop_at="result", timeout=10.0
            )

    kinds = [e["event"] for e in events]
    # The full prior transcript replays: user prompt, assistant reply, result.
    assert "user_message" in kinds
    assert "message_complete" in kinds
    assert "result" in kinds

    # And the user prompt content survived the round-trip verbatim.
    user_evt = next(e for e in events if e["event"] == "user_message")
    assert json.loads(user_evt["data"])["content"] == "what is 2+2?"


@pytest.mark.asyncio
async def test_no_event_store_means_no_cross_process_replay(tmp_path) -> None:
    """Negative control: without event_store, the resumed session's log is
    empty (just session_ready) — confirms the new store is doing the work."""
    metadata_dir = tmp_path / "sessions"
    captured_v1: list[SessionConfig] = []
    app_v1 = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_factory(captured_v1),
        metadata_store=FileSessionMetadataStore(metadata_dir),
        # event_store deliberately omitted
    )
    port_v1 = _free_port()
    sid: str

    with UvicornTestServer(app_v1, port_v1):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port_v1}", timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            await c.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message", "content": "ping"},
            )
            await _read_sse_events(c, f"/sessions/{sid}/stream", stop_at="result", timeout=10.0)

    captured_v2: list[SessionConfig] = []
    app_v2 = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_factory(captured_v2),
        metadata_store=FileSessionMetadataStore(metadata_dir),
    )
    port_v2 = _free_port()
    with UvicornTestServer(app_v2, port_v2):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port_v2}", timeout=10.0) as c:
            events = await _read_sse_events(
                c,
                f"/sessions/{sid}/stream",
                stop_at="session_ready",
                timeout=5.0,
            )

    # Just session_ready, no transcript replay — proves the event store is
    # the active ingredient.
    assert [e["event"] for e in events] == ["session_ready"]


@pytest.mark.asyncio
async def test_explicit_delete_purges_event_file(tmp_path) -> None:
    """DELETE /sessions/{id} must wipe the event JSONL — otherwise a new
    session with the same id (impossible via UUID but possible via clearing
    metadata only) would resume with a stranger's transcript."""
    metadata_dir = tmp_path / "sessions"
    app = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_factory([]),
        metadata_store=FileSessionMetadataStore(metadata_dir),
        event_store=FileSessionEventStore(metadata_dir),
    )
    port = _free_port()
    sid: str
    with UvicornTestServer(app, port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            await c.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message", "content": "x"},
            )
            await _read_sse_events(c, f"/sessions/{sid}/stream", stop_at="result", timeout=10.0)

            r = await c.delete(f"/sessions/{sid}")
            assert r.status_code == 204

    # After the with-block, uvicorn's lifespan has drained the event store.
    # The DELETE happened above, so the event file should not exist now.
    assert not (metadata_dir / f"{sid}.events.jsonl").exists()
