"""Session — long-lived holder of the SDK client + inbound queue + event log."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from . import PROTOCOL_VERSION
from .event_log import EventLog
from .sdk_bridge import (
    ConflictError,
    PermissionRouter,
    SDKClient,
    build_can_use_tool,
    translate_sdk_messages,
)

logger = logging.getLogger(__name__)


SDKFactory = Callable[..., Awaitable[SDKClient]]


class BackpressureError(Exception):
    """Raised when the session cannot accept more inbound messages right now."""


class SessionConfig:
    def __init__(
        self,
        *,
        model: Optional[str] = None,
        permission_mode: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> None:
        self.model = model
        self.permission_mode = permission_mode
        self.cwd = cwd


class Session:
    def __init__(
        self,
        session_id: str,
        client: SDKClient,
        *,
        event_log: Optional[EventLog] = None,
        router: Optional[PermissionRouter] = None,
        idle_timeout_s: float = 300.0,
    ) -> None:
        self.id = session_id
        self.client = client
        self.event_log = event_log if event_log is not None else EventLog()
        self.router = router if router is not None else PermissionRouter()
        self.idle_timeout_s = idle_timeout_s
        self._inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._tasks: list[asyncio.Task[Any]] = []
        self._closed = False
        self._last_activity = time.monotonic()
        # Gates the send loop so the next query() does not race the receive loop still
        # draining the previous turn (per spec note on interrupt). Starts SET so the very
        # first user message goes through immediately; cleared on dispatch and re-set when
        # the receive loop sees a ResultMessage (or on interrupt completing drain).
        self._turn_done: asyncio.Event = asyncio.Event()
        self._turn_done.set()

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    @property
    def idle_for(self) -> float:
        return time.monotonic() - self._last_activity

    async def start(self) -> None:
        # Emit session_ready first so subscribers see it before anything else.
        self.event_log.append("session_ready", {
            "session_id": self.id,
            "protocol_version": PROTOCOL_VERSION,
        })

        # Start the receive-side translator pulling from the SDK.
        self._tasks.append(asyncio.create_task(
            self._run_receive_loop(), name=f"session-{self.id}-recv"
        ))
        self._tasks.append(asyncio.create_task(
            self._run_send_loop(), name=f"session-{self.id}-send"
        ))

    async def _run_receive_loop(self) -> None:
        def emit(event: str, data: dict[str, Any]) -> None:
            self.event_log.append(event, data)
            # `result` marks the end of a turn — release the send loop to dispatch the
            # next queued query. Per the spec: receive_messages() must finish draining
            # before accepting the next query().
            if event == "result":
                self._turn_done.set()

        try:
            await translate_sdk_messages(self.client.receive_messages(), emit)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Receive loop crashed")
            self.event_log.append("error", {"code": "receive_loop_crashed", "message": str(e)})
        finally:
            # If the receive iterator stops (clean disconnect or crash), unblock any
            # waiter on _turn_done so the send loop can exit promptly.
            self._turn_done.set()

    async def _run_send_loop(self) -> None:
        """Pulls user messages off the inbound queue and forwards to client.query().

        Each query waits until the previous turn has drained (signaled by `result`).
        """
        while not self._closed:
            try:
                msg = await self._inbound.get()
            except asyncio.CancelledError:
                return
            try:
                await self._turn_done.wait()
                if self._closed:
                    return
                self._turn_done.clear()
                await self.client.query(msg)
                self.touch()
            except Exception as e:
                # On failure, re-open the gate so subsequent queries aren't deadlocked.
                self._turn_done.set()
                logger.exception("Failed to forward user message to SDK")
                self.event_log.append("error", {"code": "query_failed", "message": str(e)})

    # --- Inbound dispatch (called by HTTP endpoint) ---

    async def submit_user_message(self, content: Any) -> None:
        # SDK expects: {"type": "user", "message": {"role": "user", "content": ...}}
        wrapped = {"type": "user", "message": {"role": "user", "content": content}}
        try:
            self._inbound.put_nowait(wrapped)
        except asyncio.QueueFull:
            # Surface as a non-blocking error so the HTTP request can map it to 503/429
            # rather than hanging. The bound on the queue exists to apply backpressure;
            # a blocked POST handler would let one slow session take the whole worker pool.
            raise BackpressureError("Inbound queue full; refuse and retry later")
        self.touch()

    async def interrupt(self) -> None:
        await self.client.interrupt()
        self.touch()

    def resolve_permission(
        self,
        correlation_id: str,
        behavior: str,
        *,
        updated_input: Optional[dict[str, Any]] = None,
        updated_permissions: Optional[list[Any]] = None,
        message: Optional[str] = None,
        interrupt: Optional[bool] = None,
    ) -> None:
        if not self.router.has_pending(correlation_id):
            raise ConflictError("No pending permission for that correlation_id")
        self.router.resolve(correlation_id, {
            "behavior": behavior,
            "updated_input": updated_input,
            "updated_permissions": updated_permissions,
            "message": message,
            "interrupt": interrupt,
        })
        self.touch()

    def resolve_question(self, correlation_id: str, answers: Any) -> None:
        if not self.router.has_pending(correlation_id):
            raise ConflictError("No pending question for that correlation_id")
        self.router.resolve(correlation_id, answers)
        self.touch()

    async def set_permission_mode(self, mode: str) -> None:
        await self.client.set_permission_mode(mode)
        self.touch()

    async def set_model(self, model: Optional[str]) -> None:
        await self.client.set_model(model)
        self.touch()

    async def stop_task(self, task_id: str) -> None:
        await self.client.stop_task(task_id)
        self.touch()

    # --- Lifecycle ---

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.router.cancel_all()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self.client.disconnect()
        except Exception:
            logger.exception("client.disconnect failed")
        self.event_log.append("done", {})
        self.event_log.close()


class SessionRegistry:
    def __init__(self, sdk_factory: SDKFactory, *, idle_timeout_s: float = 300.0) -> None:
        self._sdk_factory = sdk_factory
        self._sessions: dict[str, Session] = {}
        self._idle_timeout_s = idle_timeout_s
        self._reaper_task: Optional[asyncio.Task[None]] = None

    def start_reaper(self) -> None:  # pragma: no cover - lifespan-managed background task
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_loop(), name="session-reaper")

    async def _reap_loop(self) -> None:  # pragma: no cover - 30s timer; tested via shutdown
        while True:
            await asyncio.sleep(30.0)
            stale = [s for s in self._sessions.values() if s.idle_for > self._idle_timeout_s]
            for s in stale:
                logger.info("Reaping idle session %s (idle=%.1fs)", s.id, s.idle_for)
                await self.remove(s.id)

    async def create(self, config: SessionConfig) -> Session:
        session_id = str(uuid.uuid4())
        # Build the per-session router and event log up front so the can_use_tool callback
        # can be constructed before the SDK client. Factories receive the callback and are
        # expected to install it via their own constructor (e.g. ClaudeAgentOptions.can_use_tool).
        event_log = EventLog()
        router = PermissionRouter()
        can_use_tool = build_can_use_tool(event_log.append, router)
        client = await self._invoke_factory(config, can_use_tool)
        session = Session(
            session_id,
            client,
            event_log=event_log,
            router=router,
            idle_timeout_s=self._idle_timeout_s,
        )
        await session.start()
        self._sessions[session_id] = session
        return session

    async def _invoke_factory(self, config: SessionConfig, can_use_tool: Any) -> SDKClient:
        """Call factory with both arguments; tolerate legacy single-arg factories."""
        try:
            return await self._sdk_factory(config, can_use_tool)
        except TypeError:
            # Backward compatibility for factories written before the callback contract.
            return await self._sdk_factory(config)

    def get(self, session_id: str) -> Optional[Session]:
        return self._sessions.get(session_id)

    async def remove(self, session_id: str) -> None:
        s = self._sessions.pop(session_id, None)
        if s is not None:
            await s.close()

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, Exception):
                pass
        for sid in list(self._sessions.keys()):
            await self.remove(sid)
