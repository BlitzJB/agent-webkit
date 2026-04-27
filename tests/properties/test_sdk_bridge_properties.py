"""Property-based tests for the SDK-bridge translation surface.

Covers:
* ``_coerce_context``: must yield a JSON-serializable dict for either a dict
  or a ToolPermissionContext-shaped dataclass, preserving fields when present
  and never raising.
* ``translate_sdk_messages``: for any synthesized message stream, the emitted
  wire-event sequence respects the documented invariants (every tool_use
  block produces both ``message_complete`` and ``tool_use``; ``ResultMessage``
  always emits exactly one ``result``).
"""
from __future__ import annotations

import json
import string
from dataclasses import dataclass
from typing import Any, Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

from agent_webkit_server.sdk_bridge import _coerce_context, translate_sdk_messages


# --- _coerce_context properties ----------------------------------------------


@dataclass
class _FakeCtx:
    tool_use_id: Optional[str] = None
    correlation_id: Optional[str] = None
    agent_id: Optional[str] = None
    suggestions: Optional[list] = None


_id_text = st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=0, max_size=20)


@settings(max_examples=120, deadline=None)
@given(
    tool_use_id=st.one_of(st.none(), _id_text),
    correlation_id=st.one_of(st.none(), _id_text),
    agent_id=st.one_of(st.none(), _id_text),
    suggestions=st.one_of(st.none(), st.lists(st.text(max_size=8), max_size=3)),
)
def test_coerce_context_dataclass_yields_json_serializable_dict(
    tool_use_id, correlation_id, agent_id, suggestions
) -> None:
    """For any dataclass ctx, output is a dict, JSON-serializable, and carries
    every non-None field — no AttributeError, no surprises."""
    ctx = _FakeCtx(
        tool_use_id=tool_use_id,
        correlation_id=correlation_id,
        agent_id=agent_id,
        suggestions=suggestions,
    )
    out = _coerce_context(ctx)
    assert isinstance(out, dict)
    json.dumps(out)  # must serialize without raising
    for name, val in (
        ("tool_use_id", tool_use_id),
        ("correlation_id", correlation_id),
        ("agent_id", agent_id),
        ("suggestions", suggestions),
    ):
        if val is not None:
            assert out[name] == val
        else:
            assert name not in out


@settings(max_examples=80, deadline=None)
@given(
    extra=st.dictionaries(st.text(max_size=6), st.integers(), max_size=4),
    tool_use_id=st.one_of(st.none(), _id_text),
)
def test_coerce_context_dict_passes_through_unchanged(extra, tool_use_id) -> None:
    """When the SDK already passed a dict, ``_coerce_context`` returns it as-is."""
    d = dict(extra)
    if tool_use_id is not None:
        d["tool_use_id"] = tool_use_id
    out = _coerce_context(d)
    assert out is d  # identity: no copy, no rebuild


# --- translate_sdk_messages properties ---------------------------------------


@dataclass
class AssistantMessage:  # noqa: D401 — name matches SDK kind for fallback classifier
    """Synthetic AssistantMessage with optional tool_use blocks."""
    id: str
    content: list
    model: Optional[str] = None
    stop_reason: Optional[str] = None


@dataclass
class ResultMessage:  # noqa: D401 — name matches SDK kind for fallback classifier
    session_id: str
    subtype: str = "success"
    total_cost_usd: Optional[float] = None


@pytest.fixture(autouse=True)
def _force_name_classifier(monkeypatch):
    """Force translate_sdk_messages to fall back to class-name classification.

    The bridge prefers ``isinstance`` against the real SDK classes when they're
    installed; with the SDK present, our local dataclasses wouldn't match. Empty
    ``_SDK_TYPES`` so classification uses ``type(msg).__name__`` — and our fakes
    are named to match.
    """
    from agent_webkit_server import sdk_bridge as _bridge
    monkeypatch.setattr(_bridge, "_SDK_TYPES", {})


async def _drive(messages: list) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []

    async def gen():
        for m in messages:
            yield m

    await translate_sdk_messages(gen(), lambda ev, data: out.append((ev, data)))
    return out


_text_block = st.builds(
    lambda t: {"type": "text", "text": t},
    st.text(max_size=20),
)
_tool_use_block = st.builds(
    lambda i, n, inp: {"type": "tool_use", "id": i, "name": n, "input": inp},
    st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=8),
    st.sampled_from(["Read", "Bash", "AskUserQuestion", "Write"]),
    st.dictionaries(st.text(max_size=4), st.integers(), max_size=3),
)


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    blocks=st.lists(st.one_of(_text_block, _tool_use_block), min_size=0, max_size=5),
    msg_id=st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=8),
)
@pytest.mark.asyncio
async def test_assistant_message_emits_one_complete_plus_tool_use_per_block(blocks, msg_id) -> None:
    """For every AssistantMessage, the bridge emits exactly one
    ``message_complete`` and one ``tool_use`` per tool_use block — and
    ``tool_use_id`` matches the block's id."""
    msg = AssistantMessage(id=msg_id, content=blocks)
    events = await _drive([msg])

    completes = [d for ev, d in events if ev == "message_complete"]
    tool_uses = [d for ev, d in events if ev == "tool_use"]
    expected_tool_blocks = [b for b in blocks if b["type"] == "tool_use"]

    assert len(completes) == 1
    assert len(tool_uses) == len(expected_tool_blocks)

    # Order + correspondence: tool_use events must mirror the tool_use blocks 1:1.
    for ev_payload, blk in zip(tool_uses, expected_tool_blocks):
        assert ev_payload["tool_use_id"] == blk["id"]
        assert ev_payload["tool_name"] == blk["name"]
        assert ev_payload["message_id"] == msg_id


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    n_assistants=st.integers(min_value=0, max_value=4),
    session_id=st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=12),
)
@pytest.mark.asyncio
async def test_result_message_terminates_with_one_result_event(n_assistants, session_id) -> None:
    """Any number of AssistantMessages followed by one ResultMessage produces
    a stream whose final event is ``result`` — exactly once."""
    msgs = [AssistantMessage(id=f"a{i}", content=[]) for i in range(n_assistants)]
    msgs.append(ResultMessage(session_id=session_id, subtype="success"))

    events = await _drive(msgs)
    kinds = [ev for ev, _ in events]

    assert kinds.count("result") == 1
    assert kinds[-1] == "result"
    # The result payload always carries the session_id we passed in.
    result_payload = next(d for ev, d in events if ev == "result")
    assert result_payload["session_id"] == session_id
