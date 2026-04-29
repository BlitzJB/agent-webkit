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
    # When set, the SDK rehydrates the transcript for this id from its bound
    # SessionStore instead of starting fresh — paired with ``create_app``'s
    # ``session_store=`` for cross-instance resume.
    resume: Optional[str] = None


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


class ArtefactCreatedData(BaseModel):
    artefact_id: str
    title: str
    kind: str
    language: Optional[str] = None
    version: int
    content: str
    summary: Optional[str] = None
    session_id: str
    created_at: int


class ArtefactUpdatedData(BaseModel):
    artefact_id: str
    version: int
    content: str
    summary: Optional[str] = None
    updated_at: int


class ArtefactDeletedData(BaseModel):
    artefact_id: str


class ReplayTruncatedData(BaseModel):
    """Synthetic event emitted when a subscriber's ``Last-Event-ID`` falls
    off the ring buffer in ``?graceful=1`` mode. The client should rehydrate
    state from the REST snapshot endpoints, then continue tailing — its next
    ``Last-Event-ID`` will be this event's ``seq`` (``oldest_available_id - 1``)
    which lands on the first event still in the ring."""

    requested_event_id: int
    oldest_available_id: int
    last_event_id: int


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
    "artefact_created",
    "artefact_updated",
    "artefact_deleted",
    "replay_truncated",
})


# --- REST endpoint response models (artefacts) ---

class ArtefactSummaryResponse(BaseModel):
    artefact_id: str
    session_id: str
    title: str
    kind: str
    language: Optional[str] = None
    current_version: int
    created_at: int
    updated_at: int


class ArtefactReadResponse(BaseModel):
    artefact_id: str
    session_id: str
    title: str
    kind: str
    language: Optional[str] = None
    current_version: int
    version: int
    content: str
    summary: Optional[str] = None
    created_at: int
    updated_at: int


class ArtefactVersionResponse(BaseModel):
    artefact_id: str
    version: int
    content: str
    summary: Optional[str] = None
    created_at: int
    created_by: str


class SnapshotResponse(BaseModel):
    """Aggregated rehydration payload — what a client fetches after a
    ``replay_truncated`` event (or on cold-start from a known session id).

    ``last_event_id`` is the wire seq the client should continue tailing from
    via ``Last-Event-ID``. ``messages`` and ``artefacts`` are session state
    that has been reconstituted from durable stores (SDK SessionStore + the
    per-session reduced message buffer in :class:`Session`, and the bound
    :class:`ArtefactStore` respectively).
    """

    session_id: str
    protocol_version: str = "1.0"
    last_event_id: int
    messages: list[dict[str, Any]] = Field(default_factory=list)
    artefacts: list[ArtefactSummaryResponse] = Field(default_factory=list)
