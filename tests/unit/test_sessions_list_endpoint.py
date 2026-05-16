"""GET /sessions — server-side index of resumable sessions.

The browser uses this to render its sidebar. It must enumerate every
session the metadata store knows about (whether or not the wrapper is
currently in memory), and return empty when the app was built without
a metadata_store.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig
from agent_webkit_server.session import SessionConfig
from agent_webkit_server.session_metadata import FileSessionMetadataStore
from tests.fake_claude_sdk import FakeClaudeSDKClient
from tests.unit.test_http_app import UvicornTestServer, _free_port


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _factory(captured: list[SessionConfig]):
    async def factory(config: SessionConfig, can_use_tool=None):
        captured.append(config)
        return FakeClaudeSDKClient(FIXTURES / "plain_qa.jsonl", can_use_tool=can_use_tool)
    return factory


@pytest.mark.asyncio
async def test_list_endpoint_returns_persisted_sessions(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path / "sessions")
    app = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_factory([]),
        metadata_store=store,
    )
    port = _free_port()
    with UvicornTestServer(app, port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as c:
            r0 = await c.get("/sessions")
            assert r0.status_code == 200
            assert r0.json() == {"sessions": []}

            # Create two sessions with distinct cwds.
            sid_a = (await c.post("/sessions", json={"cwd": "/work/a"})).json()["session_id"]
            sid_b = (await c.post("/sessions", json={"cwd": "/work/b"})).json()["session_id"]

            r = await c.get("/sessions")
            assert r.status_code == 200
            payload = r.json()
            ids = sorted(s["id"] for s in payload["sessions"])
            assert ids == sorted([sid_a, sid_b])
            by_id = {s["id"]: s for s in payload["sessions"]}
            assert by_id[sid_a]["cwd"] == "/work/a"
            assert by_id[sid_b]["cwd"] == "/work/b"
            # sdk_session_id will be None — no ResultMessage yet for these.
            assert by_id[sid_a]["sdk_session_id"] is None


@pytest.mark.asyncio
async def test_list_endpoint_returns_empty_without_metadata_store(tmp_path) -> None:
    app = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_factory([]),
        # No metadata_store — pure in-memory mode.
    )
    port = _free_port()
    with UvicornTestServer(app, port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0) as c:
            # Even after creating one, list is empty — there's nothing durable
            # to enumerate.
            await c.post("/sessions", json={})
            r = await c.get("/sessions")
            assert r.status_code == 200
            assert r.json() == {"sessions": []}


@pytest.mark.asyncio
async def test_list_endpoint_surfaces_resumed_sessions_after_restart(tmp_path) -> None:
    """After a restart, the new process's /sessions list must include sessions
    that haven't yet been touched in this process — they exist on disk."""
    metadata_dir = tmp_path / "sessions"
    captured_v1: list[SessionConfig] = []
    app_v1 = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_factory(captured_v1),
        metadata_store=FileSessionMetadataStore(metadata_dir),
    )
    port_v1 = _free_port()
    with UvicornTestServer(app_v1, port_v1):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port_v1}", timeout=5.0) as c:
            sid = (await c.post("/sessions", json={"cwd": "/work/keep"})).json()["session_id"]

    # Fresh process, same metadata dir.
    captured_v2: list[SessionConfig] = []
    app_v2 = create_app(
        auth=AuthConfig(disabled=True),
        sdk_factory=_factory(captured_v2),
        metadata_store=FileSessionMetadataStore(metadata_dir),
    )
    port_v2 = _free_port()
    with UvicornTestServer(app_v2, port_v2):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port_v2}", timeout=5.0) as c:
            r = await c.get("/sessions")
            assert r.status_code == 200
            ids = [s["id"] for s in r.json()["sessions"]]
            assert sid in ids
