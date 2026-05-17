"""End-to-end HTTP test for persistent session resume.

This is the high-fidelity version of the unit tests in test_session_resume.py:
it boots a real uvicorn server, drives the full SSE wire protocol, simulates
a process restart (tears the server down, spins a new one pointing at the
same metadata directory), and asserts that a `GET /sessions/{id}/stream`
against the *new* process picks up the *old* session id and routes through
`ClaudeAgentOptions(resume=<sdk_session_id>)` to the SDK factory.

The trick: we use a hand-rolled factory (not the real SDK) so we can capture
exactly what config the factory was invoked with on the rebuild — that's the
only way to verify the resume parameter actually flowed through end-to-end.
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
from tests.fake_claude_sdk import FakeClaudeSDKClient
from tests.unit.test_http_app import UvicornTestServer, _free_port, _read_sse_events

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _make_capturing_factory(fixture_name: str, captured: list[SessionConfig]):
    """Factory that records every SessionConfig it receives so the test can
    inspect what `resume` value was threaded through on the rebuild."""

    async def factory(config: SessionConfig, can_use_tool=None):
        captured.append(config)
        return FakeClaudeSDKClient(
            FIXTURES / f"{fixture_name}.jsonl", can_use_tool=can_use_tool
        )

    return factory


@pytest.mark.asyncio
async def test_session_survives_process_restart_via_metadata_store(tmp_path) -> None:
    metadata_dir = tmp_path / "sessions"
    metadata_store_v1 = FileSessionMetadataStore(metadata_dir)
    captured_v1: list[SessionConfig] = []

    app_v1 = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_make_capturing_factory("plain_qa", captured_v1),
        metadata_store=metadata_store_v1,
    )

    port = _free_port()
    sid: str

    # ── Phase 1: first process. Create session, drive a turn, drain to result.
    with UvicornTestServer(app_v1, port):
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            res = await c.post("/sessions", json={})
            assert res.status_code == 200, res.text
            sid = res.json()["session_id"]

            await c.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message", "content": "hi"},
            )

            events = await _read_sse_events(
                c,
                "/stream",
                stop_at="result",
                timeout=10.0,
            )

    # Factory invoked exactly once before restart.
    assert len(captured_v1) == 1
    assert captured_v1[0].resume is None  # fresh session, no resume

    # Wire payload sanity check.
    kinds = [e["event"] for e in events]
    assert "session_ready" in kinds
    assert kinds[-1] == "result"
    result_data = json.loads(events[-1]["data"])
    sdk_session_id = result_data["session_id"]
    assert sdk_session_id == "fake-1"  # from plain_qa.jsonl fixture

    # ── Disk should now hold the mapping. (Persistence is async — give it a tick.)
    for _ in range(50):
        meta = await metadata_store_v1.load(sid)
        if meta and meta.sdk_session_id == "fake-1":
            break
        await asyncio.sleep(0.02)
    assert meta is not None
    assert meta.id == sid
    assert meta.sdk_session_id == "fake-1"
    # And the file is really on disk where the next process can find it.
    assert (metadata_dir / f"{sid}.json").exists()

    # ── Phase 2: simulate a full restart. Brand-new app, new factory, new
    # in-memory registry — only the metadata directory carries over.
    metadata_store_v2 = FileSessionMetadataStore(metadata_dir)
    captured_v2: list[SessionConfig] = []
    app_v2 = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_make_capturing_factory("plain_qa", captured_v2),
        metadata_store=metadata_store_v2,
    )
    port_v2 = _free_port()

    with UvicornTestServer(app_v2, port_v2):
        base = f"http://127.0.0.1:{port_v2}"
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            # The OLD session id must still work — fetching its history
            # triggers the registry to resume from metadata (and emit
            # session_ready into the multiplexed log).
            hist = await c.get(f"/sessions/{sid}/history")
            assert hist.status_code == 200, hist.text
            events_after = await _read_sse_events(
                c,
                "/stream",
                stop_at="session_ready",
                timeout=10.0,
                session_id=sid,
            )
            # Sanity: just viewing history must NOT have spawned a new SDK
            # subprocess (lazy spawn). Triggering an interaction does —
            # and the factory then receives the resume id.
            assert captured_v2 == [], "view-only attach must not invoke factory"
            await c.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message", "content": "ping"},
            )
            # Drain through result so the factory call has fully landed.
            await _read_sse_events(
                c, "/stream", stop_at="result", timeout=10.0, session_id=sid,
            )

    # Stream connected successfully — no 404.
    assert any(e["event"] == "session_ready" for e in events_after)

    # Now the factory has been called — exactly once, with the resume id.
    assert len(captured_v2) == 1
    rebuild_cfg = captured_v2[0]
    assert rebuild_cfg.resume == "fake-1", (
        "factory should have been invoked with resume=<sdk_session_id> so the "
        "SDK loads the prior transcript instead of starting fresh"
    )

    # And session_ready carries the SAME wrapper UUID — the client doesn't
    # need to know anything happened.
    ready = next(e for e in events_after if e["event"] == "session_ready")
    assert json.loads(ready["data"])["session_id"] == sid


@pytest.mark.asyncio
async def test_resume_clamps_stale_last_event_id(tmp_path) -> None:
    """After a resume, the new event_log starts at seq 1 but the client may
    still be holding Last-Event-Id=N from before. The stream handler must
    clamp it so the subscription doesn't sit forever waiting for events with
    impossibly-large seq numbers."""
    metadata_dir = tmp_path / "sessions"
    store = FileSessionMetadataStore(metadata_dir)
    captured: list[SessionConfig] = []
    app = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_make_capturing_factory("plain_qa", captured),
        metadata_store=store,
    )

    port = _free_port()
    sid: str

    with UvicornTestServer(app, port):
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            await c.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message", "content": "go"},
            )
            await _read_sse_events(c, "/stream", stop_at="result", timeout=10.0)

    # Wait for sdk_session_id to land in metadata.
    for _ in range(50):
        meta = await store.load(sid)
        if meta and meta.sdk_session_id:
            break
        await asyncio.sleep(0.02)
    assert meta and meta.sdk_session_id == "fake-1"

    # Fresh process, same metadata. Client reconnects with a stale Last-Event-Id
    # that's way beyond anything the new event_log will ever have.
    app2 = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_make_capturing_factory("plain_qa", []),
        metadata_store=FileSessionMetadataStore(metadata_dir),
    )
    port2 = _free_port()
    with UvicornTestServer(app2, port2):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port2}", timeout=10.0) as c:
            # Touch /history to rebuild the session shell and put session_ready
            # into the multiplexed log.
            await c.get(f"/sessions/{sid}/history")
            events = await _read_sse_events(
                c,
                "/stream",
                headers={"Last-Event-ID": "9999"},
                stop_at="session_ready",
                timeout=10.0,
                session_id=sid,
            )
    # Despite the wildly-stale Last-Event-Id, session_ready was still delivered
    # (handler clamped after_seq=9999 down to 0 against the new global log).
    assert any(e["event"] == "session_ready" for e in events)


@pytest.mark.asyncio
async def test_stream_for_unknown_session_still_404s_after_restart(tmp_path) -> None:
    """Resume must not be a foot-gun: a session id we've never seen still 404s."""
    metadata_dir = tmp_path / "sessions"
    app = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_make_capturing_factory("plain_qa", []),
        metadata_store=FileSessionMetadataStore(metadata_dir),
    )
    port = _free_port()
    with UvicornTestServer(app, port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as c:
            # /history is the per-session entry point now (used to be /stream).
            r = await c.get("/sessions/00000000-0000-0000-0000-000000000000/history")
            assert r.status_code == 404


@pytest.mark.asyncio
async def test_explicit_delete_purges_metadata_preventing_resume(tmp_path) -> None:
    """DELETE /sessions/{id} is the user explicitly ending the session — the
    metadata file must be gone so subsequent stream requests 404."""
    metadata_dir = tmp_path / "sessions"
    store = FileSessionMetadataStore(metadata_dir)
    app = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_make_capturing_factory("plain_qa", []),
        metadata_store=store,
    )
    port = _free_port()
    with UvicornTestServer(app, port):
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            # Metadata exists.
            assert await store.load(sid) is not None

            r = await c.delete(f"/sessions/{sid}")
            assert r.status_code == 204

            # Metadata is gone.
            assert await store.load(sid) is None

            # And the next /history request 404s — no zombie resume.
            r = await c.get(f"/sessions/{sid}/history")
            assert r.status_code == 404
