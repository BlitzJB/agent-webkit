"""Bridge between the Claude Agent SDK (Python) and our wire protocol.

Responsibilities:
- Hold a long-lived ClaudeSDKClient per session.
- Pull SDK messages from `client.receive_messages()` and translate to outbound events.
- Implement `can_use_tool` so permissions become out-of-band SSE events that wait for an
  inbound `permission_response`.
- Hook the AskUserQuestion tool name and route through a dedicated channel.
- Convert inbound `user_message` payloads into the SDK's expected async-iterable input.

This module imports the real SDK lazily so it can be substituted for the mock in tests.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger(__name__)


# Use the real SDK's permission result types when available; otherwise fall back to
# locally-defined duck-typed equivalents. The mock SDK and the bridge test path don't care
# which is in use — they only read the same attribute names.
try:
    from claude_agent_sdk.types import (  # type: ignore
        PermissionResultAllow,
        PermissionResultDeny,
    )
except ImportError:  # pragma: no cover — used in test environments without the real SDK.
    @dataclass
    class PermissionResultAllow:  # type: ignore[no-redef]
        updated_input: Optional[dict[str, Any]] = None
        updated_permissions: Optional[list[Any]] = None

    @dataclass
    class PermissionResultDeny:  # type: ignore[no-redef]
        message: Optional[str] = None
        interrupt: bool = False


# Protocol the bridge depends on. The real ClaudeSDKClient and our fake_claude_sdk both
# satisfy this — that's how we swap them in tests.
class SDKClient(Protocol):
    async def connect(self, prompt: Any | None = None) -> None: ...
    async def query(self, prompt: Any) -> None: ...
    def receive_messages(self) -> Any: ...  # AsyncIterator
    async def interrupt(self) -> None: ...
    async def set_permission_mode(self, mode: str) -> None: ...
    async def set_model(self, model: Optional[str]) -> None: ...
    async def stop_task(self, task_id: str) -> None: ...
    async def disconnect(self) -> None: ...


class PermissionRouter:
    """Holds pending Futures for permission/question/hook decisions, keyed by correlation_id.

    First reply wins — subsequent resolve attempts raise ConflictError.
    """

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[Any]] = {}

    def register(self, correlation_id: str) -> asyncio.Future[Any]:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[correlation_id] = fut
        return fut

    def resolve(self, correlation_id: str, value: Any) -> None:
        fut = self._pending.get(correlation_id)
        if fut is None or fut.done():
            raise ConflictError(f"No pending decision for correlation_id={correlation_id} (or already resolved)")
        fut.set_result(value)
        # Keep the future around briefly for late-arrivers; pop after a tick.
        del self._pending[correlation_id]

    def has_pending(self, correlation_id: str) -> bool:
        fut = self._pending.get(correlation_id)
        return fut is not None and not fut.done()

    def cancel_all(self) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.cancel()
        self._pending.clear()


class ConflictError(Exception):
    """Raised when a permission/question response targets an already-resolved correlation_id."""


def build_can_use_tool(emit: Callable[[str, dict[str, Any]], None], router: PermissionRouter):
    """Construct a `can_use_tool` callback for the SDK.

    `emit(event_name, data)` appends a server event to the log (we lazily import the SDK
    types only inside the closure to avoid a hard dependency at import time).
    """
    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        # The real SDK passes ToolPermissionContext (a dataclass); the fake passes a dict.
        # Tolerate both — pull tool_use_id off whichever shape we got.
        ctx_dict = _coerce_context(context)
        correlation_id = ctx_dict.get("tool_use_id") or ctx_dict.get("correlation_id") or _fallback_id()

        # AskUserQuestion is special: route via dedicated event type.
        if tool_name == "AskUserQuestion":
            fut = router.register(correlation_id)
            emit("ask_user_question", {
                "correlation_id": correlation_id,
                "questions": tool_input,
            })
            answers = await fut
            # AskUserQuestion is *answered* by allowing the tool with updated_input that
            # carries the user's answers. The SDK then surfaces these as the tool's result.
            return PermissionResultAllow(updated_input={"answers": answers})

        fut = router.register(correlation_id)
        emit("permission_request", {
            "correlation_id": correlation_id,
            "tool_name": tool_name,
            "input": tool_input,
            "context": ctx_dict,
        })
        decision = await fut
        if decision.get("behavior") == "allow":
            kwargs: dict[str, Any] = {}
            if decision.get("updated_input") is not None:
                kwargs["updated_input"] = decision["updated_input"]
            if decision.get("updated_permissions") is not None:
                kwargs["updated_permissions"] = decision["updated_permissions"]
            return PermissionResultAllow(**kwargs)
        else:
            kwargs2: dict[str, Any] = {}
            if decision.get("message") is not None:
                kwargs2["message"] = decision["message"]
            if decision.get("interrupt") is not None:
                kwargs2["interrupt"] = decision["interrupt"]
            return PermissionResultDeny(**kwargs2)

    return can_use_tool


def _coerce_context(ctx: Any) -> dict[str, Any]:
    """Real SDK passes a ToolPermissionContext dataclass; the fake passes a dict.

    Returns a JSON-serializable dict either way, so the rest of the bridge can
    treat the two uniformly and the wire payload stays consistent.
    """
    if isinstance(ctx, dict):
        return ctx
    out: dict[str, Any] = {}
    for attr in ("tool_use_id", "correlation_id", "agent_id", "suggestions"):
        v = getattr(ctx, attr, None)
        if v is not None:
            out[attr] = v
    return out


_id_counter = 0


def _fallback_id() -> str:
    global _id_counter
    _id_counter += 1
    return f"corr-{_id_counter}"


_SDK_TYPES: dict[str, Any] = {}
try:  # pragma: no cover — exercised in environments with the real SDK installed.
    from claude_agent_sdk import (  # type: ignore
        AssistantMessage as _SDKAssistantMessage,
        ResultMessage as _SDKResultMessage,
        SystemMessage as _SDKSystemMessage,
        UserMessage as _SDKUserMessage,
    )
    _SDK_TYPES = {
        "AssistantMessage": _SDKAssistantMessage,
        "UserMessage": _SDKUserMessage,
        "ResultMessage": _SDKResultMessage,
        "SystemMessage": _SDKSystemMessage,
    }
except ImportError:
    _SDK_TYPES = {}


def _classify(msg: Any) -> str:
    """Map an SDK message instance to a stable kind string.

    Prefer `isinstance` against the real SDK classes when they're importable; this is
    subclass-safe. Fall back to the class name only when the SDK isn't installed (e.g.
    test environments), where the fake's class names are the contract by construction.
    """
    for kind, cls in _SDK_TYPES.items():
        if isinstance(msg, cls):
            return kind
    return type(msg).__name__


async def translate_sdk_messages(messages: Any, emit: Callable[[str, dict[str, Any]], None]) -> None:
    """Pull from the SDK's async iterator and translate to wire events."""
    async for msg in messages:
        try:
            kind = _classify(msg)
            if kind == "AssistantMessage":
                # Final assistant message — emit as message_complete.
                content = _serialize_blocks(getattr(msg, "content", []))
                msg_id = getattr(msg, "id", None) or _fallback_id()
                emit("message_complete", {
                    "message_id": msg_id,
                    "message": {
                        "id": msg_id,
                        "role": "assistant",
                        "content": content,
                        "model": getattr(msg, "model", None),
                        "stop_reason": getattr(msg, "stop_reason", None),
                    },
                })
                # Surface tool_use blocks as discrete events too, for UIs that want them.
                for blk in content:
                    if blk.get("type") == "tool_use":
                        emit("tool_use", {
                            "message_id": msg_id,
                            "tool_use_id": blk["id"],
                            "tool_name": blk["name"],
                            "input": blk.get("input", {}),
                        })
            elif kind == "PartialAssistantMessage" or kind == "AssistantMessageDelta":  # pragma: no cover - reserved for future SDK delta streaming
                content = _serialize_blocks(getattr(msg, "content", []))
                msg_id = getattr(msg, "id", None) or _fallback_id()
                for blk in content:
                    emit("message_delta", {"message_id": msg_id, "delta": blk})
            elif kind == "UserMessage":
                # Echoes of user-side messages including tool_result blocks.
                for blk in _serialize_blocks(getattr(msg, "content", [])):
                    if blk.get("type") == "tool_result":
                        emit("tool_result", {
                            "tool_use_id": blk["tool_use_id"],
                            "output": blk.get("content"),
                            "is_error": bool(blk.get("is_error", False)),
                        })
            elif kind == "ResultMessage":
                payload: dict[str, Any] = {
                    "session_id": getattr(msg, "session_id", ""),
                    "subtype": getattr(msg, "subtype", "success"),
                }
                cost = getattr(msg, "total_cost_usd", None)
                if cost is not None:
                    payload["total_cost_usd"] = cost
                emit("result", payload)
            elif kind == "SystemMessage":
                # mcp_status_change etc. live here in some SDK versions.
                subtype = getattr(msg, "subtype", "")
                if subtype == "mcp_status":
                    emit("mcp_status_change", {
                        "server_name": getattr(msg, "server_name", ""),
                        "status": getattr(msg, "status", ""),
                    })
            else:  # pragma: no cover - defensive: unknown SDK message kind
                logger.debug("Unmapped SDK message kind: %s", kind)
        except Exception as e:  # pragma: no cover - defensive: translation failure
            logger.exception("Failed to translate SDK message")
            emit("error", {"code": "translate_failed", "message": str(e)})


def _serialize_blocks(blocks: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in blocks or []:
        if isinstance(b, dict):
            out.append(b)
            continue
        # Try to coerce SDK block dataclasses → dicts
        d: dict[str, Any] = {}  # pragma: no cover - exercised only when real SDK passes dataclass blocks
        for attr in ("type", "text", "id", "name", "input", "source", "tool_use_id", "content", "is_error"):
            v = getattr(b, attr, None)
            if v is not None:
                d[attr] = v
        out.append(d)
    return out
