"""Smoke tests for the fake Claude SDK itself.

Per the design: build the mock against a known fixture format, run smoke tests to confirm
behavior, then layer the server tests on top. These tests target the fake directly so a
regression in the fake doesn't masquerade as a server bug.
"""
import asyncio
from pathlib import Path

import pytest

from tests.fake_claude_sdk import (
    AssistantMessage,
    FakeClaudeSDKClient,
    ResultMessage,
    UserMessage,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.mark.asyncio
async def test_fake_yields_assistant_then_result_for_plain_qa():
    client = FakeClaudeSDKClient(FIXTURES / "plain_qa.jsonl")
    await client.connect()
    try:
        # The fixture has expect_user_query first — push a message to satisfy it.
        await client.query({"type": "user", "message": {"role": "user", "content": "hi"}})

        msgs = []
        async for m in client.receive_messages():
            msgs.append(m)
        assert isinstance(msgs[0], AssistantMessage)
        assert msgs[0].id == "m_1"
        assert msgs[0].content[0]["text"] == "hello world"
        assert isinstance(msgs[-1], ResultMessage)
        assert msgs[-1].subtype == "success"
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_fake_blocks_on_expect_user_query_until_query_arrives():
    """The fake must not advance past expect_user_query until a query is pushed."""
    client = FakeClaudeSDKClient(FIXTURES / "plain_qa.jsonl")
    await client.connect()
    try:
        gen = client.receive_messages()

        async def first_msg():
            return await gen.__anext__()

        task = asyncio.create_task(first_msg())
        # Without a query(), this must remain pending.
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert not done

        # Now push a query — the gen should advance.
        await client.query({"type": "user", "message": {"role": "user", "content": "hi"}})
        result = await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(result, AssistantMessage)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_fake_invokes_can_use_tool_callback_for_tool_use_fixture():
    client = FakeClaudeSDKClient(FIXTURES / "read_allow.jsonl")
    await client.connect()

    invocations = []

    async def cb(name, input, context):
        invocations.append((name, input, context))
        from server.sdk_bridge import PermissionResultAllow
        return PermissionResultAllow()

    client._can_use_tool = cb  # type: ignore[attr-defined]
    try:
        await client.query({"type": "user", "message": {"role": "user", "content": "go"}})
        msgs = []
        async for m in client.receive_messages():
            msgs.append(m)
        # Read_allow fixture contains exactly one callback.
        assert len(invocations) == 1
        name, tool_input, context = invocations[0]
        assert name == "Read"
        assert tool_input["path"] == "./README.md"
        assert context["tool_use_id"] == "tu_1"
        # And we should still get the eventual ResultMessage at the end.
        assert isinstance(msgs[-1], ResultMessage)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_fake_routes_ask_user_question_through_callback():
    client = FakeClaudeSDKClient(FIXTURES / "ask_user_question.jsonl")
    await client.connect()
    invocations = []

    async def cb(name, input, context):
        invocations.append((name, input, context))
        from server.sdk_bridge import PermissionResultAllow
        return PermissionResultAllow(updated_input={"answers": [{"question": "Pick a color", "selectedOptions": ["red"]}]})

    client._can_use_tool = cb  # type: ignore[attr-defined]
    try:
        await client.query({"type": "user", "message": {"role": "user", "content": "go"}})
        msgs = []
        async for m in client.receive_messages():
            msgs.append(m)
        assert any(name == "AskUserQuestion" for (name, _i, _c) in invocations)
    finally:
        await client.disconnect()
