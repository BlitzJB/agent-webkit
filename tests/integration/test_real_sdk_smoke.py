"""Real-SDK end-to-end smoke test. Gated on ANTHROPIC_API_KEY env var.

We deliberately keep this minimal — fixtures cover the broad surface; this just confirms
the bridge wires up correctly against the live SDK.
"""
import asyncio
import os

import pytest


@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set; skipping integration smoke test",
)
@pytest.mark.asyncio
async def test_plain_qa_against_real_sdk():
    from server.main import _real_sdk_factory
    from server.session import SessionConfig, SessionRegistry

    registry = SessionRegistry(_real_sdk_factory)
    session = await registry.create(SessionConfig())
    try:
        await session.submit_user_message("Reply with exactly the word 'pong'.")

        async def wait_for_result():
            async for ev in session.event_log.subscribe(after_seq=0):
                if ev.event == "result":
                    return ev

        result = await asyncio.wait_for(wait_for_result(), timeout=60.0)
        assert result.event == "result"
    finally:
        await registry.shutdown()
