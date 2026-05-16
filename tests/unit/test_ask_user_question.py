"""AskUserQuestion: the bridge must round-trip the original `questions` when
allowing the tool, otherwise Claude Code's `AskUserQuestion` implementation
loses access to its own input and dies with `H.map` (questions.map(...)) on
the next render.

Wire shape we send to the SDK is `updated_input = { ...tool_input, answers }`
so the tool sees a complete payload, identical to what it originally got
plus the user's answers.
"""
from __future__ import annotations

from typing import Any

import pytest

from agent_webkit_server.sdk_bridge import build_can_use_tool, PermissionRouter


def _make_emit():
    captured: list[tuple[str, dict[str, Any]]] = []

    def emit(ev: str, data: dict[str, Any]) -> None:
        captured.append((ev, data))

    return captured, emit


@pytest.mark.asyncio
async def test_ask_user_question_allow_merges_questions_into_updated_input() -> None:
    captured, emit = _make_emit()
    router = PermissionRouter()
    can_use_tool = build_can_use_tool(emit, router)

    tool_input = {
        "questions": [
            {
                "question": "Project type?",
                "options": [{"label": "Web app"}, {"label": "CLI"}],
            }
        ]
    }

    # Drive the callback under a paused emit/router cycle: register the
    # question, then resolve it as the user would.
    import asyncio

    async def run_resolution() -> Any:
        # The router exposes the correlation_id back to us via the emitted event;
        # we wait one tick so emit fires before we try to resolve.
        await asyncio.sleep(0)
        ev_name, data = captured[0]
        assert ev_name == "ask_user_question"
        router.resolve(data["correlation_id"], {"Project type?": "Web app"})
        return data

    call_task = asyncio.create_task(
        can_use_tool("AskUserQuestion", tool_input, {"tool_use_id": "tu_q"})
    )
    resolve_task = asyncio.create_task(run_resolution())
    result, _ = await asyncio.gather(call_task, resolve_task)

    # The SDK gets the full original input plus user answers — questions stay
    # intact so Claude Code's tool implementation doesn't choke on `H.map`.
    assert result.updated_input == {
        "questions": tool_input["questions"],
        "answers": {"Project type?": "Web app"},
    }


@pytest.mark.asyncio
async def test_ask_user_question_answers_override_any_provided_answers_key() -> None:
    """If the model accidentally seeds an `answers` field in tool_input, the
    real user answers must take precedence — not silently get clobbered."""
    captured, emit = _make_emit()
    router = PermissionRouter()
    can_use_tool = build_can_use_tool(emit, router)

    tool_input = {
        "questions": [{"question": "X?", "options": [{"label": "A"}]}],
        "answers": {"stale": "no"},
    }

    import asyncio

    async def run_resolution() -> None:
        await asyncio.sleep(0)
        ev_name, data = captured[0]
        router.resolve(data["correlation_id"], {"X?": "A"})

    call_task = asyncio.create_task(
        can_use_tool("AskUserQuestion", tool_input, {"tool_use_id": "tu_q"})
    )
    resolve_task = asyncio.create_task(run_resolution())
    result, _ = await asyncio.gather(call_task, resolve_task)

    assert result.updated_input["answers"] == {"X?": "A"}
    assert result.updated_input["questions"] == tool_input["questions"]
