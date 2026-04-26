"""Pydantic models for inbound messages and outbound event payloads.

These mirror packages/core/src/types.ts. Keep them in sync — any change here that affects
the wire format must also be reflected there and in docs/wire-protocol.md.
"""
from __future__ import annotations

from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# --- Content blocks ---

class TextBlock(BaseModel):
    type: Literal["text"]
    text: str


class ImageSource(BaseModel):
    type: Literal["base64"]
    media_type: str
    data: str


class ImageBlock(BaseModel):
    type: Literal["image"]
    source: ImageSource


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"]
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlockContent(BaseModel):
    # Permissive: tool_result content is sometimes a string, sometimes blocks.
    model_config = {"extra": "allow"}


ContentBlock = Union[TextBlock, ImageBlock, ToolUseBlock]


# --- Inbound messages ---

class UserMessage(BaseModel):
    type: Literal["user_message"]
    content: Union[str, list[dict[str, Any]]]


class Interrupt(BaseModel):
    type: Literal["interrupt"]


class PermissionResponse(BaseModel):
    type: Literal["permission_response"]
    correlation_id: str
    behavior: Literal["allow", "deny"]
    updated_input: Optional[dict[str, Any]] = None
    updated_permissions: Optional[list[Any]] = None
    message: Optional[str] = None
    interrupt: Optional[bool] = None


class QuestionResponse(BaseModel):
    type: Literal["question_response"]
    correlation_id: str
    answers: Any


class SetPermissionMode(BaseModel):
    type: Literal["set_permission_mode"]
    mode: str


class SetModel(BaseModel):
    type: Literal["set_model"]
    model: Optional[str]


class StopTask(BaseModel):
    type: Literal["stop_task"]
    task_id: str


InboundMessage = Union[
    UserMessage,
    Interrupt,
    PermissionResponse,
    QuestionResponse,
    SetPermissionMode,
    SetModel,
    StopTask,
]


# --- Session create ---

class CreateSessionRequest(BaseModel):
    model: Optional[str] = None
    permission_mode: Optional[str] = None
    cwd: Optional[str] = None


class CreateSessionResponse(BaseModel):
    session_id: str
    protocol_version: str = "1.0"


# --- Outbound event payloads (for documentation; the event log stores dicts) ---

class SessionReadyData(BaseModel):
    session_id: str
    protocol_version: str


class MessageDeltaData(BaseModel):
    message_id: str
    delta: dict[str, Any]


class MessageCompleteData(BaseModel):
    message_id: str
    message: dict[str, Any]


class ToolUseData(BaseModel):
    message_id: str
    tool_use_id: str
    tool_name: str
    input: dict[str, Any]


class ToolResultData(BaseModel):
    tool_use_id: str
    output: Any
    is_error: bool


class PermissionRequestData(BaseModel):
    correlation_id: str
    tool_name: str
    input: dict[str, Any]
    context: Optional[dict[str, Any]] = None


class AskUserQuestionData(BaseModel):
    correlation_id: str
    questions: dict[str, Any]


class HookDecisionRequestData(BaseModel):
    correlation_id: str
    hook_event: str
    hook_input: dict[str, Any]


class ResultData(BaseModel):
    model_config = {"extra": "allow"}
    session_id: str
    subtype: str
    total_cost_usd: Optional[float] = None


class ErrorData(BaseModel):
    code: str
    message: str


class McpStatusChangeData(BaseModel):
    server_name: str
    status: str


# Names of all valid outbound events. Used for contract validation.
OUTBOUND_EVENT_NAMES: frozenset[str] = frozenset({
    "session_ready",
    "message_delta",
    "message_complete",
    "tool_use",
    "tool_result",
    "permission_request",
    "ask_user_question",
    "hook_decision_request",
    "result",
    "error",
    "mcp_status_change",
    "done",
})
