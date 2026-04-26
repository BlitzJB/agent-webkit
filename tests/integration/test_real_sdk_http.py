"""End-to-end HTTP+SSE test against a real ``ClaudeSDKClient``.

Drives the actual FastAPI app over a real uvicorn server, talking to a live
Claude session through the public wire protocol — the same surface that web
and Node clients hit. Catches FastAPI/SSE plumbing drift the direct-Session
ITs can't see.
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

# Reuse the unit-test HTTP harness — exact same uvicorn-on-random-port pattern.
from tests.unit.test_http_app import UvicornTestServer, _free_port, _read_sse_events


def _has_real_creds() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    )


pytestmark = pytest.mark.skipif(
    not _has_real_creds(),
    reason="No ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN set; skipping live SDK test",
)


@pytest.mark.asyncio
async def test_real_sdk_full_flow_over_http_sse() -> None:
    """POST /sessions → POST /input → GET /stream → assert real Claude turn."""
    from server.auth import AuthConfig
    from server.main import _real_sdk_factory, create_app

    app = create_app(auth=AuthConfig(disabled=True), sdk_factory=_real_sdk_factory)
    port = _free_port()

    with UvicornTestServer(app, port):
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=120.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]

            # Submit a user turn and read the SSE stream until result.
            await c.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message",
                      "content": "Reply with exactly the word 'pong'."},
            )

            events = await _read_sse_events(
                c, f"/sessions/{sid}/stream",
                stop_at="result", timeout=120.0,
            )

    kinds = [e["event"] for e in events]
    assert "session_ready" in kinds, kinds
    assert kinds[-1] == "result", kinds
    # The result payload includes the SDK's session_id and a non-error subtype.
    result_data = json.loads(events[-1]["data"])
    assert result_data.get("subtype") in ("success", None) or "error" not in result_data.get("subtype", "")
    assert result_data.get("session_id"), result_data
