"""Session — long-lived holder of the SDK client + inbound queue + event log."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Optional

from . import PROTOCOL_VERSION
from .event_log import EventLog, LoggedEvent
from .sdk_bridge import (
    ConflictError,
    PermissionRouter,
    SDKClient,
    build_can_use_tool,
    translate_sdk_messages,
)
from .session_metadata import SessionMetadata, SessionMetadataStore
from .transcript_replay import transcript_to_events

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
        include_partial_messages: bool = False,
        resume: Optional[str] = None,
    ) -> None:
        self.model = model
        self.permission_mode = permission_mode
        self.cwd = cwd
        # When True the SDK is asked to emit raw Anthropic stream events, which
        # the bridge translates into `message_delta` wire events so clients can
        # render assistant text token-by-token.
        self.include_partial_messages = include_partial_messages
        # SDK session id to resume — set by SessionRegistry.get_or_resume when
        # rebuilding a session whose in-memory state was lost (uvicorn restart,
        # reap, etc.). The SDK loads the prior transcript so the agent picks up
        # full context. None means "fresh session, no resume."
        self.resume = resume


class Session:
    def __init__(
        self,
        session_id: str,
        client: Optional[SDKClient] = None,
        *,
        spawn_client: Optional[Callable[[], Awaitable[SDKClient]]] = None,
        event_log: Optional[EventLog] = None,
        router: Optional[PermissionRouter] = None,
        idle_timeout_s: float = 300.0,
        on_sdk_session_id_change: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self.id = session_id
        # Client is spawned lazily on the first interaction (submit_user_message,
        # interrupt, etc.). View-only attachers — switching to a session just to
        # read its transcript — never trigger the spawn, so cold session
        # switches return instantly instead of waiting on a multi-second SDK
        # subprocess boot.
        #
        # If a client is passed directly (legacy / test path), it's installed
        # immediately and the lazy spawn is bypassed.
        if client is None and spawn_client is None:
            raise ValueError("Session requires either `client` or `spawn_client`")
        self.client: Optional[SDKClient] = client
        self._spawn_client = spawn_client
        self._start_lock: asyncio.Lock = asyncio.Lock()
        self._started: bool = client is not None
        self.event_log = event_log if event_log is not None else EventLog()
        self.router = router if router is not None else PermissionRouter()
        self.idle_timeout_s = idle_timeout_s
        # Native SDK session id, captured from the first ResultMessage. This
        # is what gets persisted in SessionMetadata so we can resume across
        # process restarts via ClaudeAgentOptions(resume=sdk_session_id).
        self.sdk_session_id: Optional[str] = None
        self._on_sdk_session_id_change = on_sdk_session_id_change
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

    def emit_ready(self) -> None:
        """Emit session_ready into the event log. Called by the registry at
        shell-construction time so attaching SSE subscribers see the protocol
        handshake immediately, without waiting for SDK spawn."""
        self.event_log.append("session_ready", {
            "session_id": self.id,
            "protocol_version": PROTOCOL_VERSION,
        })

    async def ensure_started(self) -> None:
        """Idempotent: spawn the SDK client and wire up receive/send loops
        on the first call; subsequent calls are no-ops.

        This is what makes lazy-spawn work: get_or_resume returns a shell
        synchronously, and only the first inbound action (submit_user_message,
        interrupt, etc.) actually boots the SDK subprocess.
        """
        if self._started and self._tasks:
            return
        async with self._start_lock:
            if self._started and self._tasks:
                return
            if self.client is None:
                if self._spawn_client is None:  # pragma: no cover - guarded in __init__
                    raise RuntimeError("Session has no spawn_client and no client")
                self.client = await self._spawn_client()
            self._tasks.append(asyncio.create_task(
                self._run_receive_loop(), name=f"session-{self.id}-recv"
            ))
            self._tasks.append(asyncio.create_task(
                self._run_send_loop(), name=f"session-{self.id}-send"
            ))
            self._started = True

    # Backwards-compat alias so any existing caller of start() still works.
    async def start(self) -> None:
        self.emit_ready()
        await self.ensure_started()

    async def _run_receive_loop(self) -> None:
        def emit(event: str, data: dict[str, Any]) -> None:
            self.event_log.append(event, data)
            # `result` marks the end of a turn — release the send loop to dispatch the
            # next queued query. Per the spec: receive_messages() must finish draining
            # before accepting the next query().
            if event == "result":
                self._turn_done.set()
                # Capture the SDK's native session id on first sight; trigger
                # persistence (via the registry-installed callback) so we can
                # resume across server restarts.
                sid = data.get("session_id") if isinstance(data, dict) else None
                if sid and sid != self.sdk_session_id:
                    self.sdk_session_id = sid
                    if self._on_sdk_session_id_change is not None:
                        asyncio.create_task(self._on_sdk_session_id_change(sid))

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
        # Lazy-spawn: first interaction with a cold/resumed session is what
        # triggers the SDK subprocess boot. View-only attachers never pay
        # this cost.
        await self.ensure_started()
        # Record the user turn in the event log BEFORE handing it to the SDK
        # so anyone reconnecting/attaching mid-conversation replays the full
        # transcript — assistant turns alone would look like the agent
        # talking to itself.
        self.event_log.append("user_message", {"content": content})
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
        await self.ensure_started()
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
        await self.ensure_started()
        await self.client.set_permission_mode(mode)
        self.touch()

    async def set_model(self, model: Optional[str]) -> None:
        await self.ensure_started()
        await self.client.set_model(model)
        self.touch()

    async def stop_task(self, task_id: str) -> None:
        await self.ensure_started()
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
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                logger.exception("client.disconnect failed")
        self.event_log.append("done", {})
        self.event_log.close()


class SessionRegistry:
    def __init__(
        self,
        sdk_factory: SDKFactory,
        *,
        idle_timeout_s: float = 300.0,
        metadata_store: Optional[SessionMetadataStore] = None,
    ) -> None:
        self._sdk_factory = sdk_factory
        self._sessions: dict[str, Session] = {}
        self._idle_timeout_s = idle_timeout_s
        self._reaper_task: Optional[asyncio.Task[None]] = None
        # Optional persistent store. When set, sessions survive process
        # restarts and idle reaps — get_or_resume() rebuilds them transparently
        # by passing the captured SDK session id to ClaudeAgentOptions(resume=).
        # Transcript history on resume is sourced from the SDK's on-disk
        # transcript via transcript_replay.transcript_to_events — we don't
        # maintain a duplicate event journal.
        self._metadata_store = metadata_store

    def start_reaper(self) -> None:  # pragma: no cover - lifespan-managed background task
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reap_loop(), name="session-reaper")

    async def _reap_loop(self) -> None:  # pragma: no cover - 30s timer; tested via shutdown
        while True:
            await asyncio.sleep(30.0)
            stale = [s for s in self._sessions.values() if s.idle_for > self._idle_timeout_s]
            for s in stale:
                logger.info("Reaping idle session %s (idle=%.1fs)", s.id, s.idle_for)
                # Reaper-triggered removal does NOT purge metadata; the whole
                # point of metadata is to enable resume after a reap.
                await self.remove(s.id, purge_metadata=False)

    async def create(self, config: SessionConfig) -> Session:
        session_id = str(uuid.uuid4())
        session = await self._build_session(session_id, config)
        self._sessions[session_id] = session
        # Persist the bare-bones metadata immediately so a crash before the
        # first ResultMessage still leaves us with a recoverable record
        # (sdk_session_id will be filled in once the SDK produces it).
        if self._metadata_store is not None:
            await self._metadata_store.save(SessionMetadata(
                id=session_id,
                sdk_session_id=None,
                model=config.model,
                permission_mode=config.permission_mode,
                cwd=config.cwd,
                include_partial_messages=config.include_partial_messages,
            ))
        return session

    async def _build_session(self, session_id: str, config: SessionConfig) -> Session:
        # Build the per-session router and event log up front so the can_use_tool
        # callback can be constructed before the SDK client. The factory is
        # deferred — wrapped in a closure passed as `spawn_client` — so that
        # cold session resumes return a ready-to-stream shell without paying
        # the multi-second SDK subprocess spawn. The first interaction
        # (submit_user_message, interrupt, etc.) triggers the actual spawn
        # via Session.ensure_started().
        seed: Optional[list[LoggedEvent]] = None
        if config.resume:
            # Hydrate the in-memory ring from the SDK's authoritative on-disk
            # transcript so attaching clients replay the full conversation
            # without us maintaining a duplicate journal.
            replay = await asyncio.to_thread(
                transcript_to_events, config.resume, config.cwd
            )
            if replay:
                seed = replay
        event_log = EventLog(seed=seed)
        router = PermissionRouter()
        can_use_tool = build_can_use_tool(event_log.append, router)

        async def _spawn() -> SDKClient:
            return await self._invoke_factory(config, can_use_tool)

        # When the SDK reveals its native session id (in the first ResultMessage),
        # update metadata so subsequent restarts can resume. Closes over (session_id,
        # config) so we round-trip the original options too.
        async def _on_sdk_id(sid: str) -> None:
            if self._metadata_store is None:
                return
            try:
                await self._metadata_store.save(SessionMetadata(
                    id=session_id,
                    sdk_session_id=sid,
                    model=config.model,
                    permission_mode=config.permission_mode,
                    cwd=config.cwd,
                    include_partial_messages=config.include_partial_messages,
                ))
            except Exception:  # pragma: no cover - defensive
                logger.exception("Failed to persist session metadata for %s", session_id)

        session = Session(
            session_id,
            spawn_client=_spawn,
            event_log=event_log,
            router=router,
            idle_timeout_s=self._idle_timeout_s,
            on_sdk_session_id_change=_on_sdk_id if self._metadata_store is not None else None,
        )
        # Emit session_ready synchronously so attaching SSE subscribers see it
        # immediately (no waiting on SDK spawn). The first real interaction
        # will trigger ensure_started() and spawn the SDK.
        session.emit_ready()
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

    async def list_persisted(self) -> list[SessionMetadata]:
        """Return every session known to the metadata store. Empty when no
        store is configured (in-memory mode)."""
        if self._metadata_store is None:
            return []
        return await self._metadata_store.list()

    async def get_or_resume(self, session_id: str) -> Optional[Session]:
        """Return the in-memory session, or rebuild it from persisted metadata.

        Resume rebuilds the wrapper Session under the *same* session_id with
        a fresh event_log/router/client. The SDK is given the captured
        sdk_session_id via ``ClaudeAgentOptions(resume=...)`` so the agent
        continues its prior transcript. The visible chat history on the
        client is preserved (it's purely client state).

        Returns None if the session id is unknown to both the in-memory map
        and the metadata store, or if resume fails (e.g. SDK can't find the
        transcript on disk).
        """
        existing = self._sessions.get(session_id)
        if existing is not None:
            existing.touch()
            return existing
        if self._metadata_store is None:
            return None
        metadata = await self._metadata_store.load(session_id)
        if metadata is None:
            return None
        # When sdk_session_id is None the SDK never completed a turn (typical
        # case: user opened the page, created the session, and refreshed
        # before typing anything). There's no transcript to resume, but the
        # wrapper id and config are still valid — spin up a fresh SDK client
        # under the same wrapper so the user can keep using the same session.
        config = SessionConfig(
            model=metadata.model,
            permission_mode=metadata.permission_mode,
            cwd=metadata.cwd,
            include_partial_messages=metadata.include_partial_messages,
            resume=metadata.sdk_session_id,  # may be None — factory will skip resume=
        )
        try:
            session = await self._build_session(session_id, config)
        except Exception:
            logger.exception("Failed to resume session %s", session_id)
            return None
        self._sessions[session_id] = session
        return session

    async def remove(self, session_id: str, *, purge_metadata: bool = True) -> None:
        s = self._sessions.pop(session_id, None)
        if s is not None:
            await s.close()
        if purge_metadata and self._metadata_store is not None:
            await self._metadata_store.delete(session_id)

    async def shutdown(self) -> None:
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            try:
                await self._reaper_task
            except (asyncio.CancelledError, Exception):
                pass
        for sid in list(self._sessions.keys()):
            # On shutdown we don't purge metadata — sessions should survive
            # the next process start via get_or_resume.
            await self.remove(sid, purge_metadata=False)
