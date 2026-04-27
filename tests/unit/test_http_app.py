"""End-to-end HTTP tests for the FastAPI app, run against a real uvicorn server.

We don't use httpx ASGITransport because it buffers streaming response bodies — fine for
JSON endpoints but breaks SSE. A real uvicorn server on a random port is the only way to
exercise the actual streaming behavior.

These cover the full wire protocol surface a client touches: create session, POST /input
for every message type, GET /stream with SSE parsing + Last-Event-ID resume, 404/409/401.
"""
import asyncio
import contextlib
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, AsyncIterator, Iterator, Optional

import httpx
import pytest
import uvicorn

from agent_webkit_server.auth import AuthConfig
from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.session import SessionConfig
from tests.fake_claude_sdk import FakeClaudeSDKClient

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _factory(fixture_name: str):
    async def factory(_: SessionConfig, can_use_tool=None):
        return FakeClaudeSDKClient(FIXTURES / f"{fixture_name}.jsonl", can_use_tool=can_use_tool)
    return factory


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class UvicornTestServer:
    """Run uvicorn in a background thread for the duration of a test."""

    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="off",
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.port = port

    def __enter__(self) -> "UvicornTestServer":
        self.thread.start()
        # Wait for it to be ready (started flag flips after startup).
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not start in time")

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture()
def server_factory():
    """Factory that boots a server with a chosen fixture; yields its base URL."""
    @contextlib.contextmanager
    def make(fixture_name: str, *, auth: Optional[AuthConfig] = None) -> Iterator[str]:
        app = create_app(
            auth=auth or AuthConfig(disabled=True),
            sdk_factory=_factory(fixture_name),
        )
        port = _free_port()
        with UvicornTestServer(app, port):
            yield f"http://127.0.0.1:{port}"

    return make


async def _read_sse_events(
    client: httpx.AsyncClient,
    path: str,
    headers: Optional[dict[str, str]] = None,
    *,
    stop_at: Optional[str] = None,
    max_events: int = 200,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Drive an SSE stream and decode events. Stops on `stop_at` or `max_events`."""
    events: list[dict[str, Any]] = []

    async def run() -> list[dict[str, Any]]:
        async with client.stream("GET", path, headers=headers or {}) as r:
            if r.status_code != 200:
                return events
            cur: dict[str, Any] = {"data_lines": []}
            async for raw_line in r.aiter_lines():
                line = raw_line.rstrip("\r")
                if line == "":
                    if cur.get("event") or cur["data_lines"] or cur.get("id") is not None:
                        ev = {
                            "event": cur.get("event"),
                            "id": cur.get("id"),
                            "data": "\n".join(cur["data_lines"]),
                        }
                        events.append(ev)
                        if stop_at and ev["event"] == stop_at:
                            return events
                        if len(events) >= max_events:
                            return events
                    cur = {"data_lines": []}
                    continue
                if line.startswith(":"):
                    continue
                if ":" not in line:
                    field, value = line, ""
                else:
                    field, _, value = line.partition(":")
                    if value.startswith(" "):
                        value = value[1:]
                if field == "event":
                    cur["event"] = value
                elif field == "data":
                    cur["data_lines"].append(value)
                elif field == "id":
                    cur["id"] = value
        return events

    try:
        return await asyncio.wait_for(run(), timeout=timeout)
    except asyncio.TimeoutError:
        return events


@pytest.mark.asyncio
async def test_create_session_returns_id_and_protocol_version(server_factory):
    with server_factory("plain_qa") as base:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as c:
            r = await c.post("/sessions", json={})
            assert r.status_code == 200
            body = r.json()
            assert body["protocol_version"] == "1.0"
            assert isinstance(body["session_id"], str) and len(body["session_id"]) > 0


@pytest.mark.asyncio
async def test_full_plain_qa_flow_via_http(server_factory):
    with server_factory("plain_qa") as base:
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            r = await c.post(f"/sessions/{sid}/input", json={"type": "user_message", "content": "hi"})
            assert r.status_code == 204

            events = await _read_sse_events(c, f"/sessions/{sid}/stream", stop_at="result")
            names = [e["event"] for e in events]
            assert names[0] == "session_ready"
            assert "message_complete" in names
            assert names[-1] == "result"
            ids = [int(e["id"]) for e in events if e.get("id")]
            assert ids == sorted(ids)
            assert ids[0] == 1


@pytest.mark.asyncio
async def test_permission_request_roundtrip_and_409_on_double_reply(server_factory):
    with server_factory("read_allow") as base:
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            await c.post(f"/sessions/{sid}/input", json={"type": "user_message", "content": "go"})

            events = await _read_sse_events(c, f"/sessions/{sid}/stream", stop_at="permission_request")
            perm = next(e for e in events if e["event"] == "permission_request")
            cid = json.loads(perm["data"])["correlation_id"]

            r1 = await c.post(
                f"/sessions/{sid}/input",
                json={"type": "permission_response", "correlation_id": cid, "behavior": "allow"},
            )
            assert r1.status_code == 204

            r2 = await c.post(
                f"/sessions/{sid}/input",
                json={"type": "permission_response", "correlation_id": cid, "behavior": "allow"},
            )
            assert r2.status_code == 409


@pytest.mark.asyncio
async def test_unknown_session_returns_404(server_factory):
    with server_factory("plain_qa") as base:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as c:
            r = await c.post("/sessions/does-not-exist/input", json={"type": "interrupt"})
            assert r.status_code == 404


@pytest.mark.asyncio
async def test_auth_required_returns_401_without_bearer(server_factory):
    with server_factory("plain_qa", auth=AuthConfig(token="secret")) as base:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as c:
            r = await c.post("/sessions", json={})
            assert r.status_code == 401
            r2 = await c.post("/sessions", json={}, headers={"authorization": "Bearer secret"})
            assert r2.status_code == 200


@pytest.mark.asyncio
async def test_unknown_input_type_returns_400(server_factory):
    with server_factory("plain_qa") as base:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            r = await c.post(f"/sessions/{sid}/input", json={"type": "no_such_thing"})
            assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_session_makes_session_404_after(server_factory):
    with server_factory("plain_qa") as base:
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            await c.post(f"/sessions/{sid}/input", json={"type": "user_message", "content": "x"})

            await _read_sse_events(c, f"/sessions/{sid}/stream", stop_at="result")
            r = await c.delete(f"/sessions/{sid}")
            assert r.status_code == 204
            r2 = await c.post(f"/sessions/{sid}/input", json={"type": "interrupt"})
            assert r2.status_code == 404


@pytest.mark.asyncio
async def test_malformed_last_event_id_returns_400(server_factory):
    with server_factory("plain_qa") as base:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            r = await c.get(f"/sessions/{sid}/stream", headers={"last-event-id": "not-a-number"})
            assert r.status_code == 400


@pytest.mark.asyncio
async def test_resume_with_last_event_id_skips_already_seen(server_factory):
    with server_factory("plain_qa") as base:
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            await c.post(f"/sessions/{sid}/input", json={"type": "user_message", "content": "x"})

            first = await _read_sse_events(c, f"/sessions/{sid}/stream", stop_at="result")
            ids = [e["id"] for e in first if e.get("id")]
            assert len(ids) >= 2
            resume_after = ids[-2]

            second = await _read_sse_events(
                c,
                f"/sessions/{sid}/stream",
                headers={"last-event-id": resume_after},
                stop_at="result",
            )
            for e in second:
                if e.get("id"):
                    assert int(e["id"]) > int(resume_after)
            assert any(e["event"] == "result" for e in second)
