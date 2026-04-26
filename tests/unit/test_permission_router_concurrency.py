"""Concurrency tests for PermissionRouter.

The router is the gateway through which every permission/question response flows. If
multiple subscribers race on the same correlation_id, exactly one MUST win and the rest
MUST see ConflictError — no leaked futures, no silent overwrites.

We also exercise the HTTP path with `asyncio.gather` to confirm the same invariant holds
end-to-end through the FastAPI handler.
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from server.auth import AuthConfig
from server.main import create_app
from server.sdk_bridge import ConflictError, PermissionRouter
from server.session import SessionConfig
from tests.fake_claude_sdk import FakeClaudeSDKClient

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


# ---- Pure router-level concurrency ----


@pytest.mark.asyncio
async def test_router_first_resolve_wins_under_gather():
    router = PermissionRouter()
    fut = router.register("c-1")

    results: list[Any] = []
    errors: list[Exception] = []

    async def reply(value: str) -> None:
        try:
            router.resolve("c-1", value)
            results.append(value)
        except ConflictError as e:
            errors.append(e)

    await asyncio.gather(*(reply(f"r{i}") for i in range(10)))

    assert len(results) == 1, f"Exactly one resolver should win, got {len(results)}"
    assert len(errors) == 9, f"All other resolvers must raise ConflictError, got {len(errors)}"
    assert fut.done()
    # Future leaked check: pending dict must be empty after the win.
    assert not router.has_pending("c-1")


@pytest.mark.asyncio
async def test_router_resolve_unknown_correlation_id_raises():
    router = PermissionRouter()
    with pytest.raises(ConflictError):
        router.resolve("never-registered", "x")


@pytest.mark.asyncio
async def test_router_cancel_all_releases_waiters():
    """A session being torn down mid-permission must not leave coroutines pending forever."""
    router = PermissionRouter()
    fut = router.register("c-1")

    async def waiter() -> str:
        try:
            return await fut
        except asyncio.CancelledError:
            return "cancelled"

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0)  # let the waiter park
    router.cancel_all()
    out = await asyncio.wait_for(task, timeout=1.0)
    assert out == "cancelled"
    assert not router.has_pending("c-1")


# ---- End-to-end HTTP race ----


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    def __init__(self, app: Any, port: int) -> None:
        cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.port = port

    def __enter__(self) -> "_Server":
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("uvicorn did not start")

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.mark.asyncio
async def test_permission_response_race_yields_exactly_one_204_rest_409():
    """Fire 5 simultaneous permission_response POSTs for the same correlation_id."""
    async def factory(_: SessionConfig, can_use_tool: Any = None) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(FIXTURES / "read_allow.jsonl", can_use_tool=can_use_tool)

    app = create_app(auth=AuthConfig(disabled=True), sdk_factory=factory)
    port = _free_port()
    with _Server(app, port):
        base = f"http://127.0.0.1:{port}"
        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            sid = (await c.post("/sessions", json={})).json()["session_id"]
            await c.post(f"/sessions/{sid}/input", json={"type": "user_message", "content": "go"})

            # Drain SSE up to the permission_request event so we know the router is armed.
            cid: str | None = None
            async with c.stream("GET", f"/sessions/{sid}/stream") as r:
                buf: list[str] = []
                event_name: str | None = None
                async for line in r.aiter_lines():
                    if line == "":
                        if event_name == "permission_request":
                            import json
                            data = json.loads("\n".join(buf))
                            cid = data["correlation_id"]
                            break
                        buf, event_name = [], None
                        continue
                    if line.startswith("event:"):
                        event_name = line.split(":", 1)[1].strip()
                    elif line.startswith("data:"):
                        buf.append(line.split(":", 1)[1].strip())

            assert cid is not None

            async def reply() -> int:
                r = await c.post(
                    f"/sessions/{sid}/input",
                    json={"type": "permission_response", "correlation_id": cid, "behavior": "allow"},
                )
                return r.status_code

            statuses = await asyncio.gather(*(reply() for _ in range(5)))
            wins = sum(1 for s in statuses if s == 204)
            conflicts = sum(1 for s in statuses if s == 409)
            assert wins == 1, f"Exactly one POST must win, got statuses={statuses}"
            assert conflicts == 4, f"Other POSTs must 409, got statuses={statuses}"
