"""Contract tests: every wire event payload must validate against its Pydantic model.

Drives the server through a fake SDK fixture and asserts each emitted event matches its
schema. Catches drift between the wire protocol and the implementation.
"""
import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_webkit_server.event_log import LoggedEvent
from agent_webkit_server.models import (
    AskUserQuestionData,
    ErrorData,
    HookDecisionRequestData,
    McpStatusChangeData,
    MessageCompleteData,
    MessageDeltaData,
    OUTBOUND_EVENT_NAMES,
    PermissionRequestData,
    ResultData,
    SessionReadyData,
    ToolResultData,
    ToolUseData,
)
from agent_webkit_server.session import SessionConfig, SessionRegistry
from tests.fake_claude_sdk import FakeClaudeSDKClient

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"

EVENT_MODELS = {
    "session_ready": SessionReadyData,
    "message_delta": MessageDeltaData,
    "message_complete": MessageCompleteData,
    "tool_use": ToolUseData,
    "tool_result": ToolResultData,
    "permission_request": PermissionRequestData,
    "ask_user_question": AskUserQuestionData,
    "hook_decision_request": HookDecisionRequestData,
    "result": ResultData,
    "error": ErrorData,
    "mcp_status_change": McpStatusChangeData,
    # `done` has empty payload — no model needed.
}


def validate(ev: LoggedEvent) -> None:
    assert ev.event in OUTBOUND_EVENT_NAMES, f"unknown event: {ev.event}"
    model = EVENT_MODELS.get(ev.event)
    if model is None:
        return
    try:
        model.model_validate(ev.data)
    except ValidationError as e:
        raise AssertionError(f"Event {ev.event} failed schema: {e}\nPayload: {ev.data}")


def make_factory(fixture_name: str):
    async def factory(_config, can_use_tool=None):
        return FakeClaudeSDKClient(FIXTURES / f"{fixture_name}.jsonl", can_use_tool=can_use_tool)
    return factory


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["plain_qa"])
async def test_all_events_validate(fixture: str):
    registry = SessionRegistry(make_factory(fixture))
    session = await registry.create(SessionConfig())
    try:
        await session.submit_user_message("go")

        async def collect():
            seen = []
            async for ev in session.event_log.subscribe(after_seq=0):
                seen.append(ev)
                if ev.event == "result":
                    return seen
            return seen

        events = await asyncio.wait_for(collect(), timeout=3.0)
        for ev in events:
            validate(ev)
    finally:
        await registry.shutdown()


def test_inbound_message_models_round_trip():
    """Inbound payloads from the wire-protocol doc must round-trip through Pydantic."""
    from agent_webkit_server.models import (
        Interrupt,
        PermissionResponse,
        QuestionResponse,
        SetModel,
        SetPermissionMode,
        StopTask,
        UserMessage,
    )

    samples = [
        (UserMessage, {"type": "user_message", "content": "hi"}),
        (UserMessage, {"type": "user_message", "content": [{"type": "text", "text": "hi"}]}),
        (Interrupt, {"type": "interrupt"}),
        (PermissionResponse, {"type": "permission_response", "correlation_id": "x", "behavior": "allow"}),
        (PermissionResponse, {"type": "permission_response", "correlation_id": "x", "behavior": "deny", "interrupt": True}),
        (QuestionResponse, {"type": "question_response", "correlation_id": "x", "answers": [{"question": "Q", "selectedOptions": ["red"]}]}),
        (SetPermissionMode, {"type": "set_permission_mode", "mode": "default"}),
        (SetModel, {"type": "set_model", "model": "claude-opus-4-7"}),
        (SetModel, {"type": "set_model", "model": None}),
        (StopTask, {"type": "stop_task", "task_id": "t1"}),
    ]
    for model, payload in samples:
        m = model.model_validate(payload)
        assert m.type == payload["type"]
