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
        session_id: Optional[str] = None,
    ) -> None:
        self.model = model
        self.permission_mode = permission_mode
        self.cwd = cwd
        # Pre-allocated by SessionRegistry.create() before the SDK factory is
        # invoked so factory implementations that mount per-session MCP servers
        # (e.g. the artefact tools) can scope their handlers to this id.
        self.session_id = session_id


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

        # Per-session reduced message buffer. Append-only list of completed
        # assistant/user messages, kept in arrival order. Used by
        # ``GET /sessions/{id}/messages`` (and ``/snapshot``) to rehydrate
        # state for clients that fell off the SSE ring buffer. The buffer
        # mirrors what the L2 reducer would produce; we maintain it server-side
        # because the ring buffer is bounded but a chat history should not be.
        self.messages: list[dict[str, Any]] = []

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
            # Mirror completed messages into the durable per-session buffer so
            # cold-start clients can rehydrate via /messages or /snapshot. We
            # only record `message_complete` (not deltas / tool_use / tool_result)
            # — the reducer the client would run against the ring lands on the
            # same set of "stable" messages, and we keep the buffer modest.
            if event == "message_complete":
                msg = data.get("message")
                if isinstance(msg, dict):
                    self.messages.append(msg)
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
                # The real SDK's client.query() accepts either a string prompt
                # or an async-iterable of pre-wrapped message dicts; passing a
                # bare dict raises `TypeError: 'async for' requires __aiter__`.
                # Wrap our queued dict in a single-yield async generator so
                # both the real SDK and the fake (which accepts anything)
                # work uniformly.
                async def _once(m=msg):
                    yield m
                await self.client.query(_once())
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
        # Mirror into the rehydration buffer. Assistant messages land via the
        # receive loop's `message_complete` handler; user messages have no such
        # event in the wire (the SDK echoes tool_result blocks in UserMessage,
        # which we already emit as `tool_result` events), so we synthesise
        # a user-shaped entry here. Mirrors what an L2 reducer would produce.
        self.messages.append({"role": "user", "content": content})
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
        # Pre-fill the session_id on the config so factories that mount per-session
        # MCP servers (e.g. artefact tools) can scope their handlers to it.
        config.session_id = session_id
        client = await self._invoke_factory(
            config, can_use_tool, event_log_append=event_log.append
        )
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

    async def _invoke_factory(
        self,
        config: SessionConfig,
        can_use_tool: Any,
        *,
        event_log_append: Any = None,
    ) -> SDKClient:
        """Call the factory with the richest signature it accepts.

        The contract has grown over time:
        * v1.0: ``(config)``
        * v1.1: ``(config, can_use_tool)`` — added when permissions came online.
        * v1.2: ``(config, can_use_tool, *, event_log_append=...)`` — added so
          per-session MCP servers (artefacts) can publish wire events directly.

        We use ``inspect.signature`` to pick the right shape rather than the
        old ``try/except TypeError`` chain, which couldn't distinguish a
        signature mismatch from a TypeError raised *inside* the factory body
        (which would be silently swallowed by the fallback).
        """
        import inspect

        sig = inspect.signature(self._sdk_factory)
        params = sig.parameters

        # Detect whether the factory accepts the v1.2 kwarg or **kwargs.
        accepts_event_log = (
            "event_log_append" in params
            or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        )
        # Count positional-or-keyword (and positional-only) params, excluding *args
        # and var-keyword. This tells us whether the factory wants 1 or 2 positionals.
        positional_kinds = (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        positional_params = [p for p in params.values() if p.kind in positional_kinds]
        accepts_var_pos = any(
            p.kind == inspect.Parameter.VAR_POSITIONAL for p in params.values()
        )
        n_positional = len(positional_params)

        if accepts_var_pos or n_positional >= 2:
            if accepts_event_log:
                return await self._sdk_factory(
                    config, can_use_tool, event_log_append=event_log_append
                )
            return await self._sdk_factory(config, can_use_tool)
        # v1.0 single-arg factory.
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
