"""FastAPI app entrypoint."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
from typing import Any, AsyncIterator, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .. import PROTOCOL_VERSION
from ..auth import AuthConfig, require_auth
from ..event_log import EvictedError
from ..models import (
    CreateSessionRequest,
    CreateSessionResponse,
    SessionListEntry,
    SessionListResponse,
    InboundMessage,
)
from ..sdk_bridge import ConflictError, SDKClient
from ..transcript_replay import transcript_to_events
from ..session import BackpressureError, SessionConfig, SessionRegistry

logger = logging.getLogger(__name__)


def _make_real_sdk_factory(  # pragma: no cover - requires real claude_agent_sdk; covered indirectly by integration tests
    session_store: Any = None,
    genui: Any = None,
):
    """Build the default factory, optionally pre-wired with a SessionStore and/or GenUI.

    The callback is plumbed in via ClaudeAgentOptions(can_use_tool=...) — the SDK's
    documented contract — so no post-construction mutation is required. If a
    ``session_store`` is supplied it is forwarded to ``ClaudeAgentOptions`` so the
    SDK persists conversation state through it. If a ``genui`` registry is supplied,
    the factory mounts an in-process MCP server exposing one tool per registered
    component, auto-allows those tool calls through ``can_use_tool``, and merges a
    system-prompt addendum that nudges the model to use them.
    """
    async def factory(config: SessionConfig, can_use_tool: Any) -> SDKClient:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # type: ignore

        local_can_use_tool = can_use_tool
        if genui is not None:
            from ..extras.genui import wrap_can_use_tool_for_genui

            local_can_use_tool = wrap_can_use_tool_for_genui(can_use_tool, genui)

        # Launch the CLI with --dangerously-skip-permissions so runtime
        # switching INTO bypassPermissions via set_permission_mode works.
        # The flag is a launch-time GATE (not an auto-bypass): without it,
        # the CLI rejects "Cannot set permission mode to bypassPermissions
        # because the session was not launched with --dangerously-skip-
        # permissions". With it, the session still starts in whatever
        # permission_mode the caller configured; bypass is purely opt-in
        # via a later set_permission_mode call.
        options_kwargs: dict[str, Any] = {
            "can_use_tool": local_can_use_tool,
            "extra_args": {"dangerously-skip-permissions": None},
        }
        if config.model:
            options_kwargs["model"] = config.model
        if config.permission_mode:
            options_kwargs["permission_mode"] = config.permission_mode
        if config.cwd:
            options_kwargs["cwd"] = config.cwd
        if session_store is not None:
            options_kwargs["session_store"] = session_store
        if config.include_partial_messages:
            options_kwargs["include_partial_messages"] = True
        if config.resume:
            # Set by SessionRegistry.get_or_resume when rebuilding a session
            # whose in-memory state was lost. The SDK loads the prior
            # transcript from disk and continues the conversation.
            options_kwargs["resume"] = config.resume

        if genui is not None:
            mcp_server = genui.build_mcp_server()
            options_kwargs["mcp_servers"] = {genui.server_name: mcp_server}
            options_kwargs["allowed_tools"] = genui.allowed_tool_patterns()
            addendum = genui.system_prompt_addendum()
            if addendum:
                options_kwargs["system_prompt"] = {
                    "type": "preset",
                    "preset": "claude_code",
                    "append": addendum,
                }

        options = ClaudeAgentOptions(**options_kwargs)
        client = ClaudeSDKClient(options=options)
        await client.connect()
        return client  # type: ignore[return-value]

    return factory


# Backwards-compatible default factory (no session_store, no genui).
_real_sdk_factory = _make_real_sdk_factory()


def create_app(
    *,
    auth: Optional[AuthConfig] = None,
    sdk_factory=None,
    session_store: Any = None,
    genui: Any = None,
    metadata_store: Any = None,
) -> FastAPI:
    """Build a FastAPI app exposing the agent-webkit wire protocol.

    Args:
        auth: Bearer-token auth policy. Defaults to ``AuthConfig.from_env()``.
        sdk_factory: Optional callable building each session's ``ClaudeSDKClient``.
            Override to inject custom ``ClaudeAgentOptions`` (system prompt, MCP servers,
            hooks, etc.). When supplied, ``session_store`` and ``genui`` are still wired
            up at the HTTP layer (``GET /genui/schema`` is still mounted) but the caller
            is responsible for wiring them into their own ``ClaudeAgentOptions``.
        session_store: Optional ``SessionStore`` instance forwarded to
            ``ClaudeAgentOptions(session_store=...)`` by the default factory. Use this
            with the bundled :class:`PgSessionStore` for failover-friendly setups.
        genui: Optional :class:`agent_webkit_server.extras.genui.GenUIRegistry` instance.
            When provided, ``GET /genui/schema`` is mounted (publicly readable — the
            schema is a public client-side contract, not session state), and the default
            factory mounts the registry's MCP server, auto-allows its tools through
            ``can_use_tool``, and appends a system-prompt nudge.
    """
    auth = auth or AuthConfig.from_env()
    if sdk_factory is None:
        sdk_factory = _make_real_sdk_factory(session_store=session_store, genui=genui)
    registry = SessionRegistry(sdk_factory, metadata_store=metadata_store)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        registry.start_reaper()
        try:
            yield
        finally:
            await registry.shutdown()

    app = FastAPI(title="agent-webkit reference server", version="0.1.0", lifespan=lifespan)

    auth_dep = require_auth(auth)

    if genui is not None:
        @app.get("/genui/schema")
        async def genui_schema() -> JSONResponse:
            return JSONResponse(genui.schema_payload())

    @app.get("/sessions", response_model=SessionListResponse, dependencies=[Depends(auth_dep)])
    async def list_sessions() -> SessionListResponse:
        """Enumerate every session known to the metadata store.

        Returned regardless of whether the session is currently in memory —
        the point is "what can the user resume?" The list is empty when
        ``create_app`` was built without a ``metadata_store`` (pure in-memory
        mode), since there's nothing durable to enumerate.
        """
        records = await registry.list_persisted()
        return SessionListResponse(sessions=[
            SessionListEntry(
                id=r.id,
                sdk_session_id=r.sdk_session_id,
                model=r.model,
                permission_mode=r.permission_mode,
                cwd=r.cwd,
                include_partial_messages=r.include_partial_messages,
                created_at=r.created_at,
                last_seen_at=r.last_seen_at,
            )
            for r in records
        ])

    @app.post("/sessions", response_model=CreateSessionResponse, dependencies=[Depends(auth_dep)])
    async def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
        config = SessionConfig(
            model=req.model,
            permission_mode=req.permission_mode,
            cwd=req.cwd,
            include_partial_messages=req.include_partial_messages,
        )
        s = await registry.create(config)
        return CreateSessionResponse(session_id=s.id, protocol_version=PROTOCOL_VERSION)

    @app.delete("/sessions/{session_id}", dependencies=[Depends(auth_dep)])
    async def delete_session(session_id: str) -> Response:
        await registry.remove(session_id)
        return Response(status_code=204)

    @app.get("/stream", dependencies=[Depends(auth_dep)])
    async def stream(request: Request) -> StreamingResponse:
        """Process-global multiplexed SSE.

        Every wire event from every session in this process flows over this
        single connection. Each frame's ``data`` payload is wrapped:

            {"session_id": "...", "payload": {...original event payload...}}

        so the client can dispatch by ``session_id`` into a keyed reducer.
        Standard ``Last-Event-Id`` resume against the single monotonic seq.

        There is no separate per-session stream endpoint — clients fetch
        ``GET /sessions/{id}/history`` for the past, then read this stream
        for the future.
        """
        last_event_id = request.headers.get("last-event-id")
        if last_event_id is None or last_event_id == "":
            after_seq = 0
        elif last_event_id.isdigit():
            after_seq = int(last_event_id)
        else:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be a non-negative integer")

        # Clamp a stale Last-Event-ID that's beyond our current max. Easy way
        # for a client to "give up on past" without forcing them to know our
        # internal seq state.
        if after_seq > registry.event_log.last_seq:
            after_seq = 0

        # Pre-flight: surface 412 (evicted) before opening the SSE response.
        if after_seq:
            try:
                gen_iter = registry.event_log.subscribe(after_seq).__aiter__()
                try:
                    await asyncio.wait_for(gen_iter.__anext__(), timeout=0.001)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    pass
                finally:
                    await gen_iter.aclose()
            except EvictedError as e:
                raise HTTPException(status_code=412, detail=str(e))

        async def gen() -> AsyncIterator[bytes]:
            # 2s keepalive so idle intermediaries don't drop us. Critically,
            # we DON'T use `asyncio.wait_for(sub.__anext__(), timeout=...)` —
            # that cancels the iterator on timeout, which unwinds the
            # generator's internal `await waiter.wait()` with CancelledError,
            # ends the generator, and on the next loop iteration raises
            # StopAsyncIteration immediately → stream ends → client
            # reconnects forever in a tight loop.
            #
            # Instead we ensure_future the iterator step and *poll* it with
            # asyncio.wait so the task itself is never cancelled — we just
            # observe whether it's done yet.
            # Three things to race per loop:
            #   • the next event from the global log (polled — wait_for
            #     would cancel the iterator, see comment above)
            #   • a 2s keepalive timeout (idle intermediaries)
            #   • the ASGI http.disconnect message (browser aborted)
            # The disconnect race is what makes the slot release IMMEDIATELY
            # on client abort. Without it, the server waits up to 2s for the
            # next keepalive write to break-pipe — during HMR / rapid mount
            # churn that pile-up queues subsequent requests behind zombie
            # streams on the per-origin connection slot limit.
            keepalive_interval = 2.0
            pending_task: Optional[asyncio.Task[Any]] = None
            disconnect_task: Optional[asyncio.Task[Any]] = None

            async def _wait_for_disconnect() -> None:
                while True:
                    msg = await request.receive()
                    if msg.get("type") == "http.disconnect":
                        return

            try:
                disconnect_task = asyncio.ensure_future(_wait_for_disconnect())
                sub = registry.event_log.subscribe(after_seq).__aiter__()
                while True:
                    if pending_task is None:
                        pending_task = asyncio.ensure_future(sub.__anext__())
                    done, _ = await asyncio.wait(
                        {pending_task, disconnect_task},
                        timeout=keepalive_interval,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if disconnect_task in done:
                        return
                    if not done:
                        yield b": keepalive\n\n"
                        continue
                    try:
                        ev = pending_task.result()
                    except StopAsyncIteration:
                        return
                    pending_task = None
                    envelope = {"session_id": ev.session_id, "payload": ev.data}
                    payload = (
                        f"id: {ev.seq}\n"
                        f"event: {ev.event}\n"
                        f"data: {json.dumps(envelope)}\n\n"
                    ).encode("utf-8")
                    yield payload
            except EvictedError as e:
                msg = json.dumps({"code": "evicted", "message": str(e)})
                yield f"event: error\ndata: {msg}\n\n".encode("utf-8")
            finally:
                for t in (pending_task, disconnect_task):
                    if t is not None and not t.done():
                        t.cancel()

        headers = {
            "cache-control": "no-cache",
            "connection": "keep-alive",
            "x-accel-buffering": "no",
        }
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    @app.get("/sessions/{session_id}/history", dependencies=[Depends(auth_dep)])
    async def session_history(session_id: str) -> JSONResponse:
        """One-shot snapshot of past wire events for this session, sourced
        from the SDK's on-disk transcript via :func:`transcript_to_events`.
        Clients call this on first-attach to a session to populate past
        messages, then read live future events from ``GET /stream``.

        Returns ``{"events": [{event, payload}, ...]}`` where payload is the
        same shape used inside the ``/stream`` envelope. Sessions that don't
        exist at all (no metadata, no SDK transcript) return 404; sessions
        that exist but have no transcript yet return ``{"events": []}``.
        """
        s = await registry.get_or_resume(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="Session not found")
        sdk_id = s.sdk_session_id
        if sdk_id is None and registry._metadata_store is not None:
            md = await registry._metadata_store.load(session_id)
            if md is not None:
                sdk_id = md.sdk_session_id
        cwd: Optional[str] = None
        if registry._metadata_store is not None:
            md = await registry._metadata_store.load(session_id)
            if md is not None:
                cwd = md.cwd
        events_list: list[dict[str, Any]] = []
        if sdk_id is not None:
            replay = await asyncio.to_thread(transcript_to_events, sdk_id, cwd)
            for ev in replay:
                events_list.append({"event": ev.event, "payload": ev.data})
        return JSONResponse({"events": events_list})

    @app.post("/sessions/{session_id}/input", dependencies=[Depends(auth_dep)])
    async def input_message(session_id: str, request: Request) -> Response:
        s = await registry.get_or_resume(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="Session not found")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON")
        msg_type = body.get("type")

        try:
            if msg_type == "user_message":
                await s.submit_user_message(body["content"])
            elif msg_type == "interrupt":
                await s.interrupt()
            elif msg_type == "permission_response":
                s.resolve_permission(
                    body["correlation_id"],
                    body["behavior"],
                    updated_input=body.get("updated_input"),
                    updated_permissions=body.get("updated_permissions"),
                    message=body.get("message"),
                    interrupt=body.get("interrupt"),
                )
            elif msg_type == "question_response":
                s.resolve_question(body["correlation_id"], body["answers"])
            elif msg_type == "set_permission_mode":
                try:
                    await s.set_permission_mode(body["mode"])
                except Exception as e:
                    # The SDK / CLI may refuse a mode switch for safety
                    # reasons (e.g. switching to bypassPermissions on a
                    # session that wasn't launched with the corresponding
                    # flag). Surface those as 400 with the SDK's message
                    # so the UI can show a meaningful notice instead of
                    # an opaque 500.
                    raise HTTPException(status_code=400, detail=f"set_permission_mode failed: {e}")
            elif msg_type == "set_model":
                await s.set_model(body.get("model"))
            elif msg_type == "stop_task":
                await s.stop_task(body["task_id"])
            else:
                raise HTTPException(status_code=400, detail=f"Unknown message type: {msg_type}")
        except ConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except BackpressureError as e:
            # 503 with Retry-After: client should retry after a short backoff. We don't
            # block on the inbound queue — backpressure is the request's problem.
            return JSONResponse(
                status_code=503,
                content={"detail": str(e), "code": "backpressure"},
                headers={"Retry-After": "1"},
            )
        except KeyError as e:
            raise HTTPException(status_code=400, detail=f"Missing field: {e.args[0]}")
        return Response(status_code=204)

    # Expose the registry on app.state so apps mounting on top of this FastAPI
    # instance can subscribe to the global event log, query session state, etc.
    # without needing to fork create_app. Not part of the wire protocol.
    app.state.registry = registry

    return app


def main() -> None:  # pragma: no cover - CLI entrypoint
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-auth", action="store_true", help="Disable bearer-token auth (dev only)")
    p.add_argument("--token", default=os.environ.get("AGENT_WEBKIT_TOKEN"))
    args = p.parse_args()

    auth = AuthConfig(disabled=args.no_auth, token=args.token)
    app = create_app(auth=auth)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
