"""Real-SDK end-to-end ITs covering the bridge surface against a live Claude session.

These run the actual ``ClaudeSDKClient`` (no fake) through the same
``SessionRegistry`` / ``Session`` pipeline production uses, asserting the
receive-loop translates SDK messages into wire events the way unit tests
expect against the fake. Gated on real credentials.

Deliberately scoped: a handful of smokes covering the core bridge contract.
The fake-SDK unit suite is exhaustive; these exist to catch SDK-version drift
that fakes can mask (e.g. the bare-dict regression in ``client.query()``).
"""
from __future__ import annotations

import asyncio
import os

import pytest


def _has_real_creds() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    )


pytestmark = pytest.mark.skipif(
    not _has_real_creds(),
    reason="No ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN set; skipping live SDK tests",
)


async def _wait_for_event(session, event_name: str, *, after_seq: int = 0, timeout: float = 60.0):
    async def _consume():
        async for ev in session.event_log.subscribe(after_seq=after_seq):
            if ev.event == event_name:
                return ev
        return None

    return await asyncio.wait_for(_consume(), timeout=timeout)


async def _collect_until_result(session, *, after_seq: int = 0, timeout: float = 60.0):
    """Drain events until (and including) the next ``result`` — used to span a turn."""
    collected: list = []

    async def _consume():
        async for ev in session.event_log.subscribe(after_seq=after_seq):
            collected.append(ev)
            if ev.event == "result":
                return collected
        return collected

    return await asyncio.wait_for(_consume(), timeout=timeout)


@pytest.fixture
async def real_session():
    """A SessionRegistry-managed Session backed by the real Claude SDK."""
    from agent_webkit_server.adapters.fastapi import _real_sdk_factory
    from agent_webkit_server.session import SessionConfig, SessionRegistry

    registry = SessionRegistry(_real_sdk_factory)
    session = await registry.create(SessionConfig())
    try:
        yield session
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_plain_qa_against_real_sdk(real_session) -> None:
    """Baseline: a single user turn produces session_ready + assistant content + result."""
    await real_session.submit_user_message("Reply with exactly the word 'pong'.")
    events = await _collect_until_result(real_session)

    kinds = [e.event for e in events]
    assert kinds[0] == "session_ready", kinds
    assert kinds[-1] == "result", kinds
    # Some assistant-side event must appear between ready and result —
    # don't pin the exact name (delta vs complete varies by SDK version).
    middle = set(kinds[1:-1])
    assert middle & {"message_delta", "message_complete", "tool_use"}, kinds


@pytest.mark.asyncio
async def test_multi_turn_two_results_in_one_session(real_session) -> None:
    """Two queued user messages produce two ``result`` events in order.

    Exercises the send-loop's ``_turn_done`` gate: the second query must wait
    for the first turn to drain before dispatching.
    """
    await real_session.submit_user_message("Reply with exactly: one")
    first = await _wait_for_event(real_session, "result")
    assert first is not None

    await real_session.submit_user_message("Reply with exactly: two")
    second = await _wait_for_event(real_session, "result", after_seq=first.seq)
    assert second is not None
    assert second.seq > first.seq


@pytest.mark.asyncio
async def test_interrupt_settles_session_cleanly(real_session) -> None:
    """An ``interrupt()`` mid-stream still produces a terminal ``result`` event.

    Doesn't pin the result subtype (SDK may surface ``interrupted`` or settle
    naturally if the model finished before interrupt landed) — the contract
    we care about is that the session is not left in a hung state.
    """
    await real_session.submit_user_message(
        "Count from 1 to 50 slowly, one number per line."
    )
    # Fire-and-forget the interrupt shortly after dispatch.
    await asyncio.sleep(0.5)
    try:
        await real_session.interrupt()
    except Exception:
        # Some SDK versions raise if there's nothing in flight — acceptable.
        pass

    result = await _wait_for_event(real_session, "result")
    assert result is not None


@pytest.mark.asyncio
async def test_set_permission_mode_round_trip(real_session) -> None:
    """``set_permission_mode`` survives the round-trip through the real client."""
    # No assertion on side-effects beyond "doesn't raise" — the SDK owns the mode
    # state internally; we just need the bridge to forward without error.
    await real_session.set_permission_mode("default")


@pytest.mark.asyncio
async def test_set_model_round_trip(real_session) -> None:
    """``set_model(None)`` is the documented "use default" call — must not raise."""
    await real_session.set_model(None)


@pytest.mark.asyncio
async def test_ask_user_question_round_trip(real_session) -> None:
    """The real model invokes ``AskUserQuestion`` → bridge emits the dedicated
    event → ``resolve_question`` returns the answer → turn completes.

    Validates the full first-class question pipeline against a live session,
    including that the bridge's ``can_use_tool`` correctly handles the SDK's
    ``ToolPermissionContext`` dataclass (regression: the fake passes a dict,
    the real SDK passes a dataclass — earlier versions of the bridge crashed
    on ``context.get(...)``).
    """
    await real_session.submit_user_message(
        "Call the AskUserQuestion tool now with one question 'Pick a color' "
        "and two options 'red' and 'blue'. Do not answer it yourself — wait "
        "for my response, then reply with exactly the color I chose."
    )

    answered = False

    async def drive():
        nonlocal answered
        async for ev in real_session.event_log.subscribe(after_seq=0):
            if ev.event == "ask_user_question" and not answered:
                real_session.resolve_question(
                    ev.data["correlation_id"],
                    [{"question": "Pick a color", "selectedOptions": ["red"]}],
                )
                answered = True
            if ev.event == "result":
                return

    await asyncio.wait_for(drive(), timeout=120.0)
    assert answered, "Model did not invoke AskUserQuestion"


@pytest.mark.asyncio
async def test_permission_request_allow_round_trip(real_session, tmp_path) -> None:
    """Generic ``permission_request`` → ``allow`` flow against a real tool.

    AskUserQuestion exercises a *special* branch of ``can_use_tool``; this
    covers the headline branch — Claude tries to invoke a real tool, the
    bridge emits ``permission_request``, the test allows it, the tool runs,
    and the turn completes.
    """
    target = tmp_path / "permitted.txt"
    target.write_text("MAGIC_TOKEN_42")

    await real_session.submit_user_message(
        f"Read the file at {target}. After reading, reply with exactly the "
        f"single word that appears in the file."
    )

    allowed = False

    async def drive():
        nonlocal allowed
        async for ev in real_session.event_log.subscribe(after_seq=0):
            if ev.event == "permission_request" and not allowed:
                real_session.resolve_permission(
                    ev.data["correlation_id"], "allow"
                )
                allowed = True
            if ev.event == "result":
                return

    await asyncio.wait_for(drive(), timeout=120.0)
    assert allowed, "Model did not invoke a permission-gated tool"


@pytest.mark.asyncio
async def test_permission_request_deny_with_interrupt(real_session, tmp_path) -> None:
    """``permission_request`` → ``deny`` with ``interrupt=True`` ends the turn.

    Validates the deny path threads through the bridge and the SDK accepts a
    deny verdict without leaving the session hung.
    """
    target = tmp_path / "forbidden.txt"
    target.write_text("nope")

    await real_session.submit_user_message(
        f"Read the file at {target} and tell me what's in it."
    )

    denied = False

    async def drive():
        nonlocal denied
        async for ev in real_session.event_log.subscribe(after_seq=0):
            if ev.event == "permission_request" and not denied:
                real_session.resolve_permission(
                    ev.data["correlation_id"], "deny",
                    message="Access policy forbids reading that path.",
                    interrupt=True,
                )
                denied = True
            if ev.event == "result":
                return

    await asyncio.wait_for(drive(), timeout=120.0)
    assert denied, "Model did not request permission for the read"
