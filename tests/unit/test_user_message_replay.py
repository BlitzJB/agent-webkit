"""User messages must be persisted in the event log.

The original design only put assistant outputs through the wire event
stream — user prompts went straight into the SDK's inbound queue.
That worked for live chat but broke replay: any client attaching to an
existing session saw only the assistant turns, never the prompts that
triggered them.

Now ``submit_user_message`` appends a ``user_message`` wire event before
handing the prompt to the SDK, so the ring buffer is a faithful record
of the entire transcript.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig
from agent_webkit_server.session import SessionConfig
from tests.fake_claude_sdk import FakeClaudeSDKClient
from tests.unit.test_http_app import UvicornTestServer, _free_port, _read_sse_events


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _factory(fixture: str = "plain_qa"):
    async def factory(_: SessionConfig, can_use_tool=None):
        return FakeClaudeSDKClient(FIXTURES / f"{fixture}.jsonl", can_use_tool=can_use_tool)
    return factory


@pytest.mark.asyncio
async def test_submit_user_message_appears_in_event_log() -> None:
    app = create_app(auth=AuthConfig(disabled=True), sdk_factory=_factory())
    port = _free_port()

    with UvicornTestServer(app, port):
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]

            await c.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message", "content": "hello agent"},
            )

            events = await _read_sse_events(
                c, "/stream", stop_at="result", timeout=10.0
            )

    kinds = [e["event"] for e in events]
    assert "user_message" in kinds, kinds
    import json
    user_evt = next(e for e in events if e["event"] == "user_message")
    assert json.loads(user_evt["data"])["content"] == "hello agent"
    # Crucially: the user_message must be emitted BEFORE the assistant's
    # message_complete so transcript replay reflects causal order.
    user_idx = kinds.index("user_message")
    complete_idx = kinds.index("message_complete")
    assert user_idx < complete_idx


@pytest.mark.asyncio
async def test_attach_after_user_turn_replays_user_message() -> None:
    """The whole point of the change: a *new* SSE subscriber gets the prior
    user prompt on attach, not just the assistant reply."""
    app = create_app(auth=AuthConfig(disabled=True), sdk_factory=_factory())
    port = _free_port()

    with UvicornTestServer(app, port):
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]

            # First subscriber: send the prompt, drain to result.
            await c.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message", "content": "first prompt"},
            )
            await _read_sse_events(
                c, "/stream", stop_at="result", timeout=10.0
            )

            # Second subscriber attaches fresh (no Last-Event-ID) — gets the
            # full ring buffer including the user prompt.
            events = await _read_sse_events(
                c, "/stream", stop_at="result", timeout=10.0
            )

    kinds = [e["event"] for e in events]
    assert "user_message" in kinds
    assert "message_complete" in kinds
