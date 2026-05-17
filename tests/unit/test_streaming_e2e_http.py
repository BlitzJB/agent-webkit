"""E2E test for streaming partial messages over the full HTTP+SSE wire.

Boots the FastAPI app with a fake SDK client that replays `StreamEvent`
fixtures, posts a user message, and asserts the SSE stream carries the
expected `message_delta` events in order followed by `message_complete`
with the reconciled content. This is the seam where the new server-side
StreamEvent translation meets the protocol clients see.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agent_webkit_server.auth import AuthConfig
from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.session import SessionConfig
from tests.fake_claude_sdk import FakeClaudeSDKClient
from tests.unit.test_http_app import UvicornTestServer, _free_port, _read_sse_events


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _streaming_factory():
    """Same shape as the fastapi-bundled factory, but returning a fake whose
    fixture replays StreamEvent frames before the final AssistantMessage."""

    async def factory(_: SessionConfig, can_use_tool=None):
        return FakeClaudeSDKClient(
            FIXTURES / "streaming_partial_messages.jsonl",
            can_use_tool=can_use_tool,
        )

    return factory


@pytest.mark.asyncio
async def test_stream_events_surface_as_message_delta_then_complete() -> None:
    """Full pipeline: fake SDK emits StreamEvent → bridge translates →
    FastAPI streams → SSE client decodes ordered events."""
    app = create_app(auth=AuthConfig(disabled=True), sdk_factory=_streaming_factory())
    port = _free_port()

    with UvicornTestServer(app, port):
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            res = await c.post(
                "/sessions",
                json={"include_partial_messages": True},
            )
            assert res.status_code == 200, res.text
            sid = res.json()["session_id"]

            await c.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message", "content": "go"},
            )

            events = await _read_sse_events(
                c,
                "/stream",
                stop_at="result",
                timeout=10.0,
            )

    kinds = [e["event"] for e in events]
    # message_delta must appear before message_complete, and result terminates.
    assert "session_ready" in kinds
    assert kinds[-1] == "result"
    assert "message_complete" in kinds
    delta_idxs = [i for i, k in enumerate(kinds) if k == "message_delta"]
    complete_idx = kinds.index("message_complete")
    assert delta_idxs, kinds
    assert all(i < complete_idx for i in delta_idxs), kinds

    # Each delta carries the same message_id and a text payload that
    # concatenates to the final assistant content.
    delta_payloads = [
        json.loads(e["data"]) for e in events if e["event"] == "message_delta"
    ]
    assert all(d["message_id"] == "m_str" for d in delta_payloads)
    streamed_text = "".join(d["delta"]["text"] for d in delta_payloads)
    assert streamed_text == "Hello world"

    complete_payload = json.loads(events[complete_idx]["data"])
    assert complete_payload["message_id"] == "m_str"
    assert complete_payload["message"]["content"] == [
        {"type": "text", "text": "Hello world"}
    ]
