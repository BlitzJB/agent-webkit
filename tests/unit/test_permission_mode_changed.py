"""Wire event for permission_mode changes.

set_permission_mode is an on-demand, post-creation action — it must emit
a `permission_mode_changed` event into the global log so every subscriber
(this tab and any other) sees the new mode, AND must persist the mode in
session metadata so resume across restarts comes back in the right mode.
"""
import asyncio
import time
import uuid
from pathlib import Path

import pytest

from agent_webkit_server.session import Session, SessionConfig, SessionRegistry
from agent_webkit_server.session_metadata import (
    FileSessionMetadataStore,
    SessionMetadata,
)
from tests.fake_claude_sdk import FakeClaudeSDKClient

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def make_factory(fixture_name: str):
    async def factory(config: SessionConfig, can_use_tool=None):
        return FakeClaudeSDKClient(FIXTURES / f"{fixture_name}.jsonl", can_use_tool=can_use_tool)
    return factory


class _ModeTrackingClient:
    """Minimal fake SDK that records the modes it's been switched to."""

    def __init__(self) -> None:
        self.modes_seen: list[str] = []
        self.connected = False

    async def connect(self, prompt=None) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def query(self, prompt) -> None:
        pass

    async def receive_messages(self):
        # Never yields — keeps the receive loop parked harmlessly.
        await asyncio.Event().wait()
        if False:
            yield  # type: ignore[unreachable]

    async def interrupt(self) -> None:
        pass

    async def set_permission_mode(self, mode: str) -> None:
        self.modes_seen.append(mode)

    async def set_model(self, model) -> None:
        pass

    async def stop_task(self, task_id) -> None:
        pass


async def _next_event_of(log, name: str, timeout: float = 2.0):
    async def collect():
        async for ev in log.subscribe(after_seq=0):
            if ev.event == name:
                return ev
    return await asyncio.wait_for(collect(), timeout=timeout)


@pytest.mark.asyncio
async def test_emits_permission_mode_changed_after_sdk_ack():
    """Event fires once, AFTER the SDK has acknowledged the mode switch."""
    client = _ModeTrackingClient()
    session = Session(str(uuid.uuid4()), client)

    await session.set_permission_mode("plan")

    ev = await _next_event_of(session.event_log, "permission_mode_changed")
    assert ev.session_id == session.id
    assert ev.data == {"mode": "plan"}
    # SDK was called exactly once for this transition.
    assert client.modes_seen == ["plan"]
    # And the Session's in-memory cache is updated.
    assert session.permission_mode == "plan"


@pytest.mark.asyncio
async def test_event_emitted_only_after_client_call_succeeds():
    """If the SDK call raises, no event must be emitted — subscribers would
    otherwise believe a mode is active when it isn't."""

    class _Failing(_ModeTrackingClient):
        async def set_permission_mode(self, mode: str) -> None:
            raise RuntimeError("SDK refused")

    client = _Failing()
    session = Session("sid_Y", client)

    with pytest.raises(RuntimeError):
        await session.set_permission_mode("plan")

    # No event should have been published.
    log = session.event_log
    found: list = []

    async def peek():
        async for ev in log.subscribe(after_seq=0):
            found.append(ev)

    task = asyncio.create_task(peek())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # session_ready may be present, but permission_mode_changed must NOT be.
    assert all(e.event != "permission_mode_changed" for e in found)
    assert session.permission_mode is None


@pytest.mark.asyncio
async def test_distinct_sessions_emit_with_their_own_session_id():
    """Multiplexed log — each event must carry the correct sid envelope."""
    sid_a, sid_b = str(uuid.uuid4()), str(uuid.uuid4())
    a = Session(sid_a, _ModeTrackingClient())
    b = Session(sid_b, _ModeTrackingClient())

    await a.set_permission_mode("plan")
    await b.set_permission_mode("acceptEdits")

    ea = await _next_event_of(a.event_log, "permission_mode_changed")
    eb = await _next_event_of(b.event_log, "permission_mode_changed")
    assert ea.session_id == sid_a
    assert ea.data == {"mode": "plan"}
    assert eb.session_id == sid_b
    assert eb.data == {"mode": "acceptEdits"}


@pytest.mark.asyncio
async def test_registry_persists_mode_to_metadata(tmp_path):
    """Toggling mode on a session whose sdk_session_id is already captured
    must rewrite the metadata file so a restart resumes in the new mode."""
    store = FileSessionMetadataStore(tmp_path)
    registry = SessionRegistry(make_factory("plain_qa"), metadata_store=store)
    session = await registry.create(SessionConfig(permission_mode="default"))
    sid = session.id
    # The registry only persists once the SDK reports a session id; we
    # short-circuit by pre-seeding metadata for `sid` so the _on_mode
    # callback finds something to rewrite.
    await store.save(SessionMetadata(
        id=sid,
        sdk_session_id="sdk-xyz",
        model=None,
        permission_mode="default",
        cwd=None,
        include_partial_messages=False,
    ))

    await session.set_permission_mode("plan")

    reloaded = await store.load(sid)
    assert reloaded is not None
    assert reloaded.permission_mode == "plan"

    await registry.shutdown()


@pytest.mark.asyncio
async def test_entering_plan_captures_prior_mode():
    """Going INTO plan from acceptEdits must capture the prior mode so it
    can be restored after ExitPlanMode is approved."""
    client = _ModeTrackingClient()
    session = Session(str(uuid.uuid4()), client)

    await session.set_permission_mode("acceptEdits")
    await session.set_permission_mode("plan")

    assert session._pre_plan_mode == "acceptEdits"
    assert session.permission_mode == "plan"


@pytest.mark.asyncio
async def test_re_entering_plan_does_not_clobber_prior():
    """If set_permission_mode('plan') is called while already in plan,
    the captured prior must NOT be overwritten to 'plan' (which would
    create an infinite-restore loop)."""
    client = _ModeTrackingClient()
    session = Session(str(uuid.uuid4()), client)

    await session.set_permission_mode("bypassPermissions")
    await session.set_permission_mode("plan")
    await session.set_permission_mode("plan")  # idempotent toggle

    assert session._pre_plan_mode == "bypassPermissions"


@pytest.mark.asyncio
async def test_plan_from_default_captures_default():
    """If we were in 'default' (the absence-of-mode) before plan, we still
    capture something so restore goes back to default explicitly."""
    client = _ModeTrackingClient()
    session = Session(str(uuid.uuid4()), client)
    # No prior set_permission_mode call: permission_mode is None.
    await session.set_permission_mode("plan")
    assert session._pre_plan_mode == "default"


@pytest.mark.asyncio
async def test_exit_plan_approval_restores_prior_mode_end_to_end():
    """ExitPlanMode approve → the bridge schedules a restore → emits a
    second permission_mode_changed event carrying the prior mode."""
    from agent_webkit_server.sdk_bridge import build_can_use_tool

    client = _ModeTrackingClient()
    sid = str(uuid.uuid4())
    session = Session(sid, client)
    await session.set_permission_mode("acceptEdits")
    await session.set_permission_mode("plan")
    # Manually mirror what registry.create() does to wire the callback.
    async def _on_exit_plan_approved():
        prior = session._pre_plan_mode
        if not prior:
            return
        session._pre_plan_mode = None
        await session.set_permission_mode(prior)

    def emit(event: str, data) -> None:
        session.event_log.append(sid, event, data)

    # Bind the bridge to the SESSION's router so resolve_permission below
    # actually wakes up our pending can_use_tool future. Using a fresh
    # router would silently no-op the resolve and the test would hang.
    can_use_tool = build_can_use_tool(emit, session.router, on_exit_plan_approved=_on_exit_plan_approved)

    # Kick the can_use_tool flow for ExitPlanMode, resolve allow, observe
    # the restore event.
    ctx = {"tool_use_id": "corr_plan_1"}

    async def drive_decision() -> None:
        await asyncio.sleep(0.01)  # let can_use_tool register the future
        session.resolve_permission("corr_plan_1", "allow")

    asyncio.create_task(drive_decision())
    result = await can_use_tool("ExitPlanMode", {"plan": "step 1\nstep 2"}, ctx)
    # The allow itself returned successfully.
    assert result is not None

    # Wait for the post-approval restore to complete.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if session.permission_mode == "acceptEdits":
            break
    assert session.permission_mode == "acceptEdits"
    assert session._pre_plan_mode is None

    # Two permission_mode_changed events total: into plan, then back out.
    events: list = []
    async def collect():
        async for ev in session.event_log.subscribe(after_seq=0):
            if ev.event == "permission_mode_changed":
                events.append(ev.data["mode"])
                if len(events) >= 3:  # acceptEdits, plan, acceptEdits
                    return
    await asyncio.wait_for(collect(), timeout=2.0)
    # The 3rd one is the restore.
    assert events[-1] == "acceptEdits"


@pytest.mark.asyncio
async def test_exit_plan_denied_does_not_restore():
    """Deny keeps the agent in plan mode — no restore, no mode-changed event."""
    from agent_webkit_server.sdk_bridge import build_can_use_tool

    client = _ModeTrackingClient()
    sid = str(uuid.uuid4())
    session = Session(sid, client)
    await session.set_permission_mode("acceptEdits")
    await session.set_permission_mode("plan")

    called = {"n": 0}

    async def _on_exit_plan_approved():
        called["n"] += 1

    def emit(event: str, data) -> None:
        session.event_log.append(sid, event, data)

    # Bind the bridge to the SESSION's router so resolve_permission below
    # actually wakes up our pending can_use_tool future. Using a fresh
    # router would silently no-op the resolve and the test would hang.
    can_use_tool = build_can_use_tool(emit, session.router, on_exit_plan_approved=_on_exit_plan_approved)
    ctx = {"tool_use_id": "corr_plan_deny"}

    async def drive_decision() -> None:
        await asyncio.sleep(0.01)
        session.resolve_permission("corr_plan_deny", "deny")

    asyncio.create_task(drive_decision())
    await can_use_tool("ExitPlanMode", {"plan": "x"}, ctx)
    await asyncio.sleep(0.05)
    assert called["n"] == 0
    assert session.permission_mode == "plan"
    assert session._pre_plan_mode == "acceptEdits"  # still captured


@pytest.mark.asyncio
async def test_non_exit_plan_approve_does_not_trigger_restore():
    """Approving any other tool does NOT fire the plan-restore path even
    while we happen to be in plan mode."""
    from agent_webkit_server.sdk_bridge import build_can_use_tool

    client = _ModeTrackingClient()
    sid = str(uuid.uuid4())
    session = Session(sid, client)
    await session.set_permission_mode("acceptEdits")
    await session.set_permission_mode("plan")

    called = {"n": 0}

    async def _on_exit_plan_approved():
        called["n"] += 1

    def emit(event: str, data) -> None:
        session.event_log.append(sid, event, data)

    # Bind the bridge to the SESSION's router so resolve_permission below
    # actually wakes up our pending can_use_tool future. Using a fresh
    # router would silently no-op the resolve and the test would hang.
    can_use_tool = build_can_use_tool(emit, session.router, on_exit_plan_approved=_on_exit_plan_approved)

    async def drive_decision() -> None:
        await asyncio.sleep(0.01)
        session.resolve_permission("corr_read", "allow")

    asyncio.create_task(drive_decision())
    await can_use_tool("Read", {"path": "x"}, {"tool_use_id": "corr_read"})
    await asyncio.sleep(0.05)
    assert called["n"] == 0
    assert session.permission_mode == "plan"


@pytest.mark.asyncio
async def test_no_metadata_store_means_no_persistence_but_event_still_fires():
    """Mode change must work even when no metadata store is configured
    (in-memory mode). The event is the source of truth in that case."""
    registry = SessionRegistry(make_factory("plain_qa"))  # no metadata_store
    session = await registry.create(SessionConfig())
    try:
        # Drive the session enough to spawn the SDK client.
        await session.submit_user_message("hello")
        # First wait for the SDK to be up.
        await asyncio.sleep(0.05)

        await session.set_permission_mode("plan")
        ev = await _next_event_of(session.event_log, "permission_mode_changed")
        assert ev.data["mode"] == "plan"
    finally:
        await registry.shutdown()
