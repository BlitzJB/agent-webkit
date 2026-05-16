"""Bridge must stamp a `type` field onto serialized SDK content blocks.

The SDK exposes blocks as dataclasses (TextBlock, ToolUseBlock, ToolResultBlock,
ThinkingBlock) whose Python class encodes the type — there is no `type`
attribute. The wire protocol, however, expects `type` so clients can branch
on `block.type === "tool_use"` etc. Without this stamping, tool_use blocks
arrive at the L2 reducer with `{id, name, input}` but no `type`, and React
chat UIs render them as empty bubbles.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class _FakeToolResultBlock:
    tool_use_id: str
    content: Any = None
    is_error: bool = False


@dataclass
class _FakeThinkingBlock:
    thinking: str
    signature: str


# Force the bridge to treat fakes the same way it treats real SDK classes by
# matching class names (the bridge's class-name → type mapping is the only
# stable handle when the real SDK isn't importable).
class TextBlock(_FakeTextBlock):
    pass


class ToolUseBlock(_FakeToolUseBlock):
    pass


class ToolResultBlock(_FakeToolResultBlock):
    pass


class ThinkingBlock(_FakeThinkingBlock):
    pass


def test_text_block_serializes_with_type_text() -> None:
    from agent_webkit_server.sdk_bridge import _serialize_blocks

    out = _serialize_blocks([TextBlock(text="hello")])
    assert out == [{"type": "text", "text": "hello"}]


def test_tool_use_block_serializes_with_type_tool_use() -> None:
    from agent_webkit_server.sdk_bridge import _serialize_blocks

    out = _serialize_blocks([ToolUseBlock(id="tu_1", name="Read", input={"path": "/x"})])
    assert out == [
        {"type": "tool_use", "id": "tu_1", "name": "Read", "input": {"path": "/x"}}
    ]


def test_tool_result_block_serializes_with_type_tool_result() -> None:
    from agent_webkit_server.sdk_bridge import _serialize_blocks

    out = _serialize_blocks(
        [ToolResultBlock(tool_use_id="tu_1", content=[{"type": "text", "text": "ok"}])]
    )
    assert out == [
        {
            "type": "tool_result",
            "tool_use_id": "tu_1",
            "content": [{"type": "text", "text": "ok"}],
            # is_error=False is the dataclass default and is preserved as-is;
            # the client treats it as falsy regardless.
            "is_error": False,
        }
    ]


def test_thinking_block_serializes_with_type_thinking() -> None:
    from agent_webkit_server.sdk_bridge import _serialize_blocks

    out = _serialize_blocks([ThinkingBlock(thinking="...", signature="sig")])
    assert out == [{"type": "thinking", "thinking": "...", "signature": "sig"}]


def test_dict_blocks_pass_through_unchanged() -> None:
    """Pre-formed dicts (the common case in fixtures and tests) must not be
    re-stamped — they already carry their authoritative type."""
    from agent_webkit_server.sdk_bridge import _serialize_blocks

    block = {"type": "text", "text": "verbatim", "extra": 1}
    out = _serialize_blocks([block])
    assert out == [block]


def test_unknown_dataclass_falls_back_without_type() -> None:
    """An unrecognized block class shouldn't crash — just emit whatever
    attributes are present so debugging stays possible."""
    from agent_webkit_server.sdk_bridge import _serialize_blocks

    @dataclass
    class WeirdBlock:
        text: str

    out = _serialize_blocks([WeirdBlock(text="?")])
    # type is omitted since we don't know what it is — must not raise.
    assert out == [{"text": "?"}]


@pytest.mark.asyncio
async def test_assistant_message_complete_carries_tool_use_with_type() -> None:
    """End-to-end check via translate_sdk_messages: an AssistantMessage with a
    ToolUseBlock dataclass surfaces on the wire with `type: 'tool_use'` so the
    L2 chat UI can render it."""
    from agent_webkit_server.sdk_bridge import translate_sdk_messages

    @dataclass
    class AssistantMessage:
        id: str
        content: list
        model: str = ""
        stop_reason: str = ""

    msg = AssistantMessage(
        id="m_x",
        content=[
            TextBlock(text="checking weather"),
            ToolUseBlock(id="tu_1", name="get_weather", input={"city": "Boston"}),
        ],
    )

    emitted: list[tuple[str, dict]] = []

    async def aiter():
        yield msg

    def emit(ev: str, data: dict) -> None:
        emitted.append((ev, data))

    await translate_sdk_messages(aiter(), emit)

    complete = next(d for e, d in emitted if e == "message_complete")
    assert complete["message"]["content"] == [
        {"type": "text", "text": "checking weather"},
        {"type": "tool_use", "id": "tu_1", "name": "get_weather", "input": {"city": "Boston"}},
    ]
