"""Drop-in fake of ClaudeSDKClient that replays JSONL fixtures.

Same async surface as the real SDK so the server-side bridge can't tell the difference.

Fixture format (one JSON object per line):
  {"kind": "outbound", "message": {"type": "AssistantMessage", ...}}
  {"kind": "callback_expect", "tool_name": "Read", "tool_input": {...}, "context": {...}, "respond": {"behavior": "allow"}}
  {"kind": "delay", "seconds": 0.05}
  {"kind": "expect_user_query"}   # waits for the next client.query() call before continuing

The fake exposes the same Protocol shape that sdk_bridge.SDKClient expects.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)


@dataclass
class FakeAssistantMessage:
    id: str
    content: list[dict[str, Any]]
    model: Optional[str] = None
    stop_reason: Optional[str] = None


@dataclass
class FakeUserMessage:
    content: list[dict[str, Any]]


@dataclass
class FakeResultMessage:
    session_id: str
    subtype: str = "success"
    total_cost_usd: Optional[float] = None


@dataclass
class FakeSystemMessage:
    subtype: str
    server_name: str = ""
    status: str = ""


# Type-name-based mapping in sdk_bridge uses `type(msg).__name__` — match those names.
class AssistantMessage(FakeAssistantMessage):  # type: ignore[misc]
    pass


class UserMessage(FakeUserMessage):  # type: ignore[misc]
    pass


class ResultMessage(FakeResultMessage):  # type: ignore[misc]
    pass


class SystemMessage(FakeSystemMessage):  # type: ignore[misc]
    pass


class FakeClaudeSDKClient:
    """Mock implementation matching sdk_bridge.SDKClient Protocol."""

    def __init__(
        self,
        fixture_path: Path | str,
        *,
        can_use_tool: Any | None = None,
    ) -> None:
        self._fixture_path = Path(fixture_path)
        self._inbound_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._connected = False
        # Callback may be injected via constructor (preferred — matches the real SDK's
        # ClaudeAgentOptions(can_use_tool=...) contract) or assigned after construction
        # for low-level smoke tests that drive the fake directly.
        self._can_use_tool = can_use_tool

    async def connect(self, prompt: Any | None = None) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def query(self, prompt: Any) -> None:
        await self._inbound_queue.put(prompt)

    async def interrupt(self) -> None:
        await self._inbound_queue.put({"__interrupt__": True})

    async def set_permission_mode(self, mode: str) -> None:
        pass

    async def set_model(self, model: Optional[str]) -> None:
        pass

    async def stop_task(self, task_id: str) -> None:
        pass

    async def receive_messages(self) -> AsyncIterator[Any]:
        # Read fixture lines lazily.
        with self._fixture_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                kind = entry.get("kind")

                if kind == "expect_user_query":
                    # Block until a user message arrives.
                    await self._inbound_queue.get()
                elif kind == "delay":
                    await asyncio.sleep(float(entry.get("seconds", 0)))
                elif kind == "outbound":
                    msg = entry["message"]
                    yield _coerce(msg)
                elif kind == "callback_expect":
                    if self._can_use_tool is None:
                        raise RuntimeError("Fixture wants a callback but no can_use_tool installed")
                    await self._can_use_tool(
                        entry["tool_name"],
                        entry.get("tool_input", {}),
                        entry.get("context", {"tool_use_id": entry.get("tool_use_id", "tu_x")}),
                    )
                else:
                    logger.warning("Unknown fixture kind: %s", kind)


def _coerce(msg: dict[str, Any]) -> Any:
    msg_type = msg.get("type", "AssistantMessage")
    payload = {k: v for k, v in msg.items() if k != "type"}
    if msg_type == "AssistantMessage":
        return AssistantMessage(
            id=payload.get("id", "m_x"),
            content=payload.get("content", []),
            model=payload.get("model"),
            stop_reason=payload.get("stop_reason"),
        )
    if msg_type == "UserMessage":
        return UserMessage(content=payload.get("content", []))
    if msg_type == "ResultMessage":
        return ResultMessage(
            session_id=payload.get("session_id", "fake"),
            subtype=payload.get("subtype", "success"),
            total_cost_usd=payload.get("total_cost_usd"),
        )
    if msg_type == "SystemMessage":
        return SystemMessage(
            subtype=payload.get("subtype", ""),
            server_name=payload.get("server_name", ""),
            status=payload.get("status", ""),
        )
    raise ValueError(f"Unknown fixture message type: {msg_type}")
