"""Server-side unit tests using the fake SDK (no API key required).

These exercise: queue handling, SSE event ordering, correlation-id lifecycle,
permission RPC roundtrip, and basic flow.
"""
import asyncio
from pathlib import Path

import pytest

from agent_webkit_server.session import SessionConfig, SessionRegistry
from tests.fake_claude_sdk import FakeClaudeSDKClient

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def make_factory(fixture_name: str):
    async def factory(config: SessionConfig, can_use_tool=None):
        return FakeClaudeSDKClient(FIXTURES / f"{fixture_name}.jsonl", can_use_tool=can_use_tool)
    return factory


@pytest.mark.asyncio
async def test_submit_user_message_raises_backpressure_when_queue_full():
    """Bound queue must refuse fast, not block, so an HTTP handler can map it to 503."""
    from agent_webkit_server.session import BackpressureError, Session

    class StuckClient:
        async def connect(self, prompt=None): pass
        async def disconnect(self): pass
        async def query(self, prompt):
            await asyncio.sleep(3600)  # never returns — simulates a wedged SDK
        async def receive_messages(self):
            await asyncio.sleep(3600)
            if False: yield  # type: ignore[unreachable]
        async def interrupt(self): pass
        async def set_permission_mode(self, mode): pass
        async def set_model(self, model): pass
        async def stop_task(self, task_id): pass

    s = Session("sid", StuckClient())
    # Fill the queue (default maxsize=128). The send loop is not started, so nothing drains.
    for i in range(128):
        await s.submit_user_message(f"msg-{i}")
    with pytest.raises(BackpressureError):
        await s.submit_user_message("overflow")


async def _drain_until(log, seq_target: int, timeout: float = 2.0):
    out = []
    async def collect():
        async for ev in log.subscribe(after_seq=0):
            out.append(ev)
            if ev.seq >= seq_target:
                return
    await asyncio.wait_for(collect(), timeout=timeout)
    return out


@pytest.mark.asyncio
async def test_plain_qa_emits_session_ready_and_complete_and_result():
    registry = SessionRegistry(make_factory("plain_qa"))
    session = await registry.create(SessionConfig())
    try:
        await session.submit_user_message("anything")

        events = []
        async def collect():
            async for ev in session.event_log.subscribe(after_seq=0):
                events.append(ev)
                if ev.event in ("done", "result"):
                    if ev.event == "result":
                        return
        await asyncio.wait_for(collect(), timeout=3.0)

        names = [e.event for e in events]
        assert names[0] == "session_ready"
        assert "message_complete" in names
        assert names[-1] == "result"
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_read_allow_roundtrip():
    registry = SessionRegistry(make_factory("read_allow"))
    session = await registry.create(SessionConfig())
    try:
        await session.submit_user_message("read it")

        # Wait until the permission_request appears, then approve.
        async def wait_for_perm():
            async for ev in session.event_log.subscribe(after_seq=0):
                if ev.event == "permission_request":
                    return ev
        ev = await asyncio.wait_for(wait_for_perm(), timeout=3.0)
        assert ev.data["tool_name"] == "Read"
        session.resolve_permission(ev.data["correlation_id"], "allow")

        # Drain until result.
        async def wait_for_result():
            async for e in session.event_log.subscribe(after_seq=ev.seq):
                if e.event == "result":
                    return e
        result = await asyncio.wait_for(wait_for_result(), timeout=3.0)
        assert result.event == "result"
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_resolving_unknown_correlation_id_raises():
    from agent_webkit_server.sdk_bridge import ConflictError

    registry = SessionRegistry(make_factory("plain_qa"))
    session = await registry.create(SessionConfig())
    try:
        with pytest.raises(ConflictError):
            session.resolve_permission("nope", "allow")
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_double_resolve_raises_conflict():
    from agent_webkit_server.sdk_bridge import ConflictError

    registry = SessionRegistry(make_factory("read_allow"))
    session = await registry.create(SessionConfig())
    try:
        await session.submit_user_message("go")

        async def wait_for_perm():
            async for ev in session.event_log.subscribe(after_seq=0):
                if ev.event == "permission_request":
                    return ev
        ev = await asyncio.wait_for(wait_for_perm(), timeout=3.0)
        cid = ev.data["correlation_id"]
        session.resolve_permission(cid, "allow")
        with pytest.raises(ConflictError):
            session.resolve_permission(cid, "allow")
    finally:
        await registry.shutdown()
