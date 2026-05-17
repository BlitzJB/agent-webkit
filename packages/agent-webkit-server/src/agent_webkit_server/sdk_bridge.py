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
        PermissionUpdate as _SDKPermissionUpdate,
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

    _SDKPermissionUpdate = None  # type: ignore[assignment]


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


def build_can_use_tool(
    emit: Callable[[str, dict[str, Any]], None],
    router: PermissionRouter,
    on_exit_plan_approved: Callable[[], "asyncio.Future[Any] | Any"] | None = None,
):
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
            # AskUserQuestion is answered by allowing the tool with updated_input
            # carrying the user's answers. Critically, we must preserve the
            # original tool_input (which contains the `questions` array) — the
            # tool's call() destructures `{questions, answers}` from its input,
            # and a missing `questions` makes its renderer crash with
            # `undefined.map(...)`. User answers take precedence over any
            # `answers` key the model may have seeded in tool_input.
            merged: dict[str, Any] = dict(tool_input or {})
            merged["answers"] = answers
            return PermissionResultAllow(updated_input=merged)

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
                # The wire carries dicts (see _coerce_context above), but the
                # SDK expects PermissionUpdate dataclasses and later calls
                # `.to_dict()` on each. Hydrate them back here — failing to
                # do so manifests as: `'dict' object has no attribute 'to_dict'`
                # the moment a user clicks an "Always allow" suggestion chip.
                kwargs["updated_permissions"] = [
                    _hydrate_permission_update(p) for p in decision["updated_permissions"]
                ]
            # ExitPlanMode special-case: the SDK auto-flips itself to
            # "default" on approval, but the user was likely in
            # acceptEdits/bypassPermissions before entering plan. Fire a
            # post-approval task to restore the pre-plan mode. Scheduled
            # (not awaited) so we don't block our own return — the SDK
            # processes our allow first, then our restore.
            if tool_name == "ExitPlanMode" and on_exit_plan_approved is not None:
                asyncio.create_task(_run_post_exit_plan(on_exit_plan_approved))
            return PermissionResultAllow(**kwargs)
        else:
            # Claude's API rejects a tool_result with is_error=true and
            # empty content (HTTP 400). The SDK forwards our `message`
            # into that content, so we MUST provide a non-empty default
            # when the client didn't send one — otherwise an empty deny
            # nukes the next turn.
            message = decision.get("message")
            if not isinstance(message, str) or not message.strip():
                message = "Denied by user."
            kwargs2: dict[str, Any] = {"message": message}
            if decision.get("interrupt") is not None:
                kwargs2["interrupt"] = decision["interrupt"]
            return PermissionResultDeny(**kwargs2)

    return can_use_tool


async def _run_post_exit_plan(cb: Callable[[], "asyncio.Future[Any] | Any"]) -> None:
    # Yield once so our PermissionResultAllow gets returned and the SDK
    # has a chance to apply its internal "exit plan → default" switch
    # before we attempt to restore the pre-plan mode on top of it.
    await asyncio.sleep(0)
    try:
        result = cb()
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            await result
    except Exception:
        logger.exception("ExitPlanMode post-approval callback failed")


def _coerce_context(ctx: Any) -> dict[str, Any]:
    """Real SDK passes a ToolPermissionContext dataclass; the fake passes a dict.

    Returns a JSON-serializable dict either way, so the rest of the bridge can
    treat the two uniformly and the wire payload stays consistent. Nested
    dataclasses (e.g. ``ToolPermissionContext.suggestions: list[PermissionUpdate]``)
    are deep-coerced — otherwise ``json.dumps`` on the resulting
    ``permission_request`` event raises mid-stream and the client falls into
    an auto-reconnect loop replaying the same poisoned event from the ring
    buffer.
    """
    if isinstance(ctx, dict):
        # Even passthrough dicts may contain nested dataclasses (e.g. a caller
        # built the context manually from real SDK objects). Walk it.
        return {k: _to_jsonable(v) for k, v in ctx.items()}
    out: dict[str, Any] = {}
    for attr in ("tool_use_id", "correlation_id", "agent_id", "suggestions"):
        v = getattr(ctx, attr, None)
        if v is not None:
            out[attr] = _to_jsonable(v)
    return out


def _to_jsonable(value: Any) -> Any:
    """Best-effort recursive coercion to JSON-native types.

    Plain primitives pass through. Lists/tuples and dicts recurse. Objects
    that expose ``to_dict()`` get their canonical dict form (this is how SDK
    types like ``PermissionUpdate`` round-trip into the control-protocol
    shape with camelCase keys). Other dataclasses fall back to
    ``dataclasses.asdict``. Unknown objects degrade to ``str(value)`` so
    we never crash the SSE generator.
    """
    import dataclasses

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    # Prefer an explicit `to_dict()` — that's the SDK's intended wire shape
    # (e.g. PermissionUpdate.to_dict produces camelCase rule keys that
    # round-trip cleanly through PermissionUpdate.from_dict).
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            return _to_jsonable(to_dict())
        except Exception:  # pragma: no cover - defensive
            pass
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        try:
            return _to_jsonable(dataclasses.asdict(value))
        except Exception:  # pragma: no cover - asdict can choke on non-dc fields
            return {f.name: _to_jsonable(getattr(value, f.name, None)) for f in dataclasses.fields(value)}
    if hasattr(value, "__dict__"):
        return {k: _to_jsonable(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def _hydrate_permission_update(value: Any) -> Any:
    """Convert a wire-shape dict back into an SDK ``PermissionUpdate``.

    The client sends suggestions back verbatim from what we emitted via
    ``_coerce_context`` — which used ``PermissionUpdate.to_dict()``, so the
    dict already matches the SDK's control-protocol shape. We pass it
    through ``PermissionUpdate.from_dict`` to rebuild the dataclass the SDK
    expects. If the SDK isn't importable (tests) or the value is already a
    PermissionUpdate, return it unchanged.
    """
    if _SDKPermissionUpdate is None:
        return value
    if isinstance(value, _SDKPermissionUpdate):
        return value
    if isinstance(value, dict):
        try:
            return _SDKPermissionUpdate.from_dict(value)
        except Exception:  # pragma: no cover - defensive: malformed input
            return value
    return value


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
        StreamEvent as _SDKStreamEvent,
        SystemMessage as _SDKSystemMessage,
        UserMessage as _SDKUserMessage,
    )
    _SDK_TYPES = {
        "AssistantMessage": _SDKAssistantMessage,
        "UserMessage": _SDKUserMessage,
        "ResultMessage": _SDKResultMessage,
        "SystemMessage": _SDKSystemMessage,
        "StreamEvent": _SDKStreamEvent,
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
    # Streaming state. The SDK's StreamEvent stream interleaves message_start /
    # content_block_start / content_block_delta / ... events; we need a tiny
    # state machine to attribute deltas to the right message_id and tool_use_id.
    cur_message_id: Optional[str] = None
    open_blocks: dict[int, dict[str, Any]] = {}

    async for msg in messages:
        try:
            kind = _classify(msg)
            if kind == "StreamEvent":
                ev = getattr(msg, "event", None) or {}
                etype = ev.get("type")
                if etype == "message_start":
                    cur_message_id = ((ev.get("message") or {}).get("id")) or cur_message_id
                    open_blocks.clear()
                elif etype == "content_block_start":
                    idx = ev.get("index")
                    blk = ev.get("content_block") or {}
                    if idx is not None:
                        open_blocks[idx] = blk
                elif etype == "content_block_delta":
                    if cur_message_id is None:
                        continue
                    delta = ev.get("delta") or {}
                    dtype = delta.get("type")
                    if dtype == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            emit("message_delta", {
                                "message_id": cur_message_id,
                                "delta": {"type": "text", "text": text},
                            })
                    elif dtype == "input_json_delta":
                        idx = ev.get("index")
                        blk = open_blocks.get(idx) if idx is not None else None
                        forwarded = dict(delta)
                        if blk and blk.get("type") == "tool_use":
                            forwarded.setdefault("tool_use_id", blk.get("id"))
                            if blk.get("name") is not None:
                                forwarded.setdefault("name", blk["name"])
                        emit("message_delta", {
                            "message_id": cur_message_id,
                            "delta": forwarded,
                        })
                    # other delta types (e.g. thinking_delta, signature_delta) are
                    # not part of the wire protocol yet — silently ignore.
                # ping / message_delta(stop_reason) / message_stop / content_block_stop
                # don't produce wire events — message_complete carries the final state.
                continue
            if kind == "AssistantMessage":
                # Final assistant message — emit as message_complete. If the SDK
                # didn't populate an id (common path), prefer the id we observed
                # on the most recent `message_start` StreamEvent so the L2
                # reducer can reconcile this `message_complete` with the
                # streamed `message_delta`s instead of rendering a duplicate.
                content = _serialize_blocks(getattr(msg, "content", []))
                msg_id = getattr(msg, "id", None) or cur_message_id or _fallback_id()
                # The id is consumed; the next streamed message gets its own
                # `message_start` so don't accidentally reuse this id later.
                cur_message_id = None
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


# SDK content-block dataclasses encode their type via the Python class itself
# (TextBlock, ToolUseBlock, ...) and do NOT carry a `type` attribute. The wire
# protocol — and every client — expects an explicit `type`, so we recover it
# from the class name on the way out. Class-name lookup (rather than isinstance
# against the imported SDK) keeps this working when fixtures use look-alike
# fakes whose classes mirror the SDK names but aren't subclasses.
_BLOCK_TYPE_BY_CLASS_NAME: dict[str, str] = {
    "TextBlock": "text",
    "ThinkingBlock": "thinking",
    "ToolUseBlock": "tool_use",
    "ToolResultBlock": "tool_result",
    "ServerToolUseBlock": "server_tool_use",
    "ServerToolResultBlock": "server_tool_result",
}


def _serialize_blocks(blocks: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in blocks or []:
        if isinstance(b, dict):
            out.append(b)
            continue
        # Coerce SDK block dataclasses → dicts.
        d: dict[str, Any] = {}
        inferred_type = _BLOCK_TYPE_BY_CLASS_NAME.get(type(b).__name__)
        if inferred_type is not None:
            d["type"] = inferred_type
        for attr in (
            "type",
            "text",
            "thinking",
            "signature",
            "id",
            "name",
            "input",
            "source",
            "tool_use_id",
            "content",
            "is_error",
        ):
            v = getattr(b, attr, None)
            if v is not None:
                d[attr] = v
        out.append(d)
    return out
