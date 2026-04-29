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
    ArtefactReadResponse,
    ArtefactSummaryResponse,
    ArtefactVersionResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    InboundMessage,
    SnapshotResponse,
)
from ..sdk_bridge import ConflictError, SDKClient
from ..session import BackpressureError, SessionConfig, SessionRegistry

logger = logging.getLogger(__name__)


def _make_real_sdk_factory(  # pragma: no cover - requires real claude_agent_sdk; covered indirectly by integration tests
    session_store: Any = None,
    genui: Any = None,
    artefact_store: Any = None,
):
    """Build the default factory, optionally pre-wired with extras.

    Each opt-in feature plumbs through ``ClaudeAgentOptions`` — the SDK's
    documented contract — so no post-construction mutation is required:

    * ``session_store`` → ``ClaudeAgentOptions(session_store=...)``
    * ``genui`` → mounts the registry's MCP server, auto-allows its tools, and
      appends a system-prompt nudge.
    * ``artefact_store`` → mounts a *per-session* MCP server exposing the four
      artefact tools (closures over ``session_id`` + ``event_log_append`` so
      mutations land in the right session and emit wire events), auto-allows
      those tools, and appends the artefact system prompt addendum.

    When both ``genui`` and ``artefact_store`` are supplied, their MCP servers,
    allow lists, and system-prompt addenda are merged.
    """
    async def factory(
        config: SessionConfig,
        can_use_tool: Any,
        *,
        event_log_append: Any = None,
    ) -> SDKClient:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # type: ignore

        local_can_use_tool = can_use_tool
        mcp_servers: dict[str, Any] = {}
        allowed_tools: list[str] = []
        prompt_chunks: list[str] = []

        if genui is not None:
            from ..extras.genui import wrap_can_use_tool_for_genui

            local_can_use_tool = wrap_can_use_tool_for_genui(local_can_use_tool, genui)
            mcp_servers[genui.server_name] = genui.build_mcp_server()
            allowed_tools.extend(genui.allowed_tool_patterns())
            addendum = genui.system_prompt_addendum()
            if addendum:
                prompt_chunks.append(addendum)

        if artefact_store is not None:
            from ..extras.artefacts import (
                ARTEFACT_SERVER_NAME,
                ARTEFACT_TOOL_NAMES,
                build_artefact_mcp_server,
                system_prompt_addendum as artefact_prompt,
                wrap_can_use_tool_for_artefacts,
            )

            if event_log_append is None:
                raise RuntimeError(
                    "artefact_store requires the registry to pass event_log_append "
                    "(SessionRegistry v1.2+); upgrade or drop artefact_store."
                )
            local_can_use_tool = wrap_can_use_tool_for_artefacts(local_can_use_tool)
            mcp_servers[ARTEFACT_SERVER_NAME] = build_artefact_mcp_server(
                store=artefact_store,
                session_id=config.session_id or "",
                emit=event_log_append,
            )
            allowed_tools.extend(
                f"mcp__{ARTEFACT_SERVER_NAME}__{n}" for n in ARTEFACT_TOOL_NAMES
            )
            prompt_chunks.append(artefact_prompt())

        options_kwargs: dict[str, Any] = {"can_use_tool": local_can_use_tool}
        if config.model:
            options_kwargs["model"] = config.model
        if config.permission_mode:
            options_kwargs["permission_mode"] = config.permission_mode
        if config.cwd:
            options_kwargs["cwd"] = config.cwd
        if session_store is not None:
            options_kwargs["session_store"] = session_store
        if mcp_servers:
            options_kwargs["mcp_servers"] = mcp_servers
        if allowed_tools:
            options_kwargs["allowed_tools"] = allowed_tools
        if prompt_chunks:
            options_kwargs["system_prompt"] = {
                "type": "preset",
                "preset": "claude_code",
                "append": "\n\n".join(prompt_chunks),
            }

        options = ClaudeAgentOptions(**options_kwargs)
        client = ClaudeSDKClient(options=options)
        await client.connect()
        return client  # type: ignore[return-value]

    return factory


# Backwards-compatible default factory (no extras).
_real_sdk_factory = _make_real_sdk_factory()


def create_app(
    *,
    auth: Optional[AuthConfig] = None,
    sdk_factory=None,
    session_store: Any = None,
    genui: Any = None,
    artefact_store: Any = None,
) -> FastAPI:
    """Build a FastAPI app exposing the agent-webkit wire protocol.

    Args:
        auth: Bearer-token auth policy. Defaults to ``AuthConfig.from_env()``.
        sdk_factory: Optional callable building each session's ``ClaudeSDKClient``.
            Override to inject custom ``ClaudeAgentOptions`` (system prompt, MCP servers,
            hooks, etc.). When supplied, ``session_store``, ``genui``, and
            ``artefact_store`` are still wired up at the HTTP layer (REST endpoints
            mounted) but the caller is responsible for wiring them into their own
            ``ClaudeAgentOptions``.
        session_store: Optional ``SessionStore`` instance forwarded to
            ``ClaudeAgentOptions(session_store=...)`` by the default factory.
        genui: Optional :class:`agent_webkit_server.extras.genui.GenUIRegistry`.
        artefact_store: Optional :class:`agent_webkit_server.extras.artefacts.ArtefactStore`.
            When provided, the artefact MCP tools are mounted per-session, the four
            REST endpoints (list/read/list-versions/read-version) and the cross-cutting
            ``/messages`` + ``/snapshot`` endpoints are exposed, and the SSE stream
            accepts ``?graceful=1`` so eviction is signalled with a ``replay_truncated``
            event instead of a 412.
    """
    auth = auth or AuthConfig.from_env()
    if sdk_factory is None:
        sdk_factory = _make_real_sdk_factory(
            session_store=session_store,
            genui=genui,
            artefact_store=artefact_store,
        )
    registry = SessionRegistry(sdk_factory)

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

    @app.post("/sessions", response_model=CreateSessionResponse, dependencies=[Depends(auth_dep)])
    async def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
        config = SessionConfig(
            model=req.model,
            permission_mode=req.permission_mode,
            cwd=req.cwd,
        )
        s = await registry.create(config)
        return CreateSessionResponse(session_id=s.id, protocol_version=PROTOCOL_VERSION)

    @app.delete("/sessions/{session_id}", dependencies=[Depends(auth_dep)])
    async def delete_session(session_id: str) -> Response:
        await registry.remove(session_id)
        return Response(status_code=204)

    @app.get("/sessions/{session_id}/stream", dependencies=[Depends(auth_dep)])
    async def stream(
        session_id: str,
        request: Request,
        graceful: int = 0,
    ) -> StreamingResponse:
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="Session not found")
        last_event_id = request.headers.get("last-event-id")
        if last_event_id is None or last_event_id == "":
            after_seq = 0
        elif last_event_id.isdigit():
            after_seq = int(last_event_id)
        else:
            # Reject malformed headers loudly rather than silently restarting from 0 —
            # silent fallback would replay the entire stream and produce duplicate events
            # in clients that thought they were resuming.
            raise HTTPException(status_code=400, detail="Last-Event-ID must be a non-negative integer")

        graceful_mode = bool(graceful)

        # Pre-flight: in strict mode, if the cursor is evicted, return 412 before
        # opening the stream. In graceful mode we skip this — the synthetic
        # `replay_truncated` event flows through as the first frame instead.
        if after_seq and not graceful_mode:
            try:
                gen_iter = s.event_log.subscribe(after_seq).__aiter__()
                try:
                    await asyncio.wait_for(gen_iter.__anext__(), timeout=0.001)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    pass
                finally:
                    await gen_iter.aclose()
            except EvictedError as e:
                raise HTTPException(status_code=412, detail=str(e))

        async def gen() -> AsyncIterator[bytes]:
            keepalive_interval = 15.0
            try:
                sub = s.event_log.subscribe(
                    after_seq, graceful=graceful_mode
                ).__aiter__()
                while True:
                    try:
                        ev = await asyncio.wait_for(sub.__anext__(), timeout=keepalive_interval)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    except StopAsyncIteration:
                        return
                    payload = (
                        f"id: {ev.seq}\n"
                        f"event: {ev.event}\n"
                        f"data: {json.dumps(ev.data)}\n\n"
                    ).encode("utf-8")
                    yield payload
                    if ev.event == "done":
                        return
            except EvictedError as e:
                msg = json.dumps({"code": "evicted", "message": str(e)})
                yield f"event: error\ndata: {msg}\n\n".encode("utf-8")

        headers = {
            "cache-control": "no-cache",
            "connection": "keep-alive",
            "x-accel-buffering": "no",
        }
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)

    @app.post("/sessions/{session_id}/input", dependencies=[Depends(auth_dep)])
    async def input_message(session_id: str, request: Request) -> Response:
        s = registry.get(session_id)
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
                await s.set_permission_mode(body["mode"])
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

    # ----- Rehydration / snapshot endpoints -----------------------------------
    #
    # These are the bridge between the bounded SSE ring and persistent state.
    # A client whose `Last-Event-ID` falls off the ring receives a
    # `replay_truncated` synthetic event (in `?graceful=1` mode); it then hits
    # these endpoints to rebuild state and resumes tailing from the synthetic
    # event's seq.

    def _ensure_session(session_id: str):
        s = registry.get(session_id)
        if s is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return s

    @app.get("/sessions/{session_id}/messages", dependencies=[Depends(auth_dep)])
    async def get_messages(session_id: str) -> JSONResponse:
        s = _ensure_session(session_id)
        # Snapshot of completed messages in arrival order. Cheap copy — list
        # assignment, not a deep clone — because messages are dicts the client
        # treats as read-only (they came off the wire).
        return JSONResponse(
            {
                "session_id": session_id,
                "last_event_id": s.event_log.last_seq,
                "messages": list(s.messages),
            }
        )

    if artefact_store is not None:
        from ..extras.artefacts import ArtefactNotFoundError

        def _artefact_summary(a: Any) -> ArtefactSummaryResponse:
            return ArtefactSummaryResponse(
                artefact_id=a.id,
                session_id=a.session_id,
                title=a.title,
                kind=a.kind,
                language=a.language,
                current_version=a.current_version,
                created_at=a.created_at,
                updated_at=a.updated_at,
            )

        def _artefact_read(a: Any, v: Any) -> ArtefactReadResponse:
            return ArtefactReadResponse(
                artefact_id=a.id,
                session_id=a.session_id,
                title=a.title,
                kind=a.kind,
                language=a.language,
                current_version=a.current_version,
                version=v.version,
                content=v.content,
                summary=v.summary,
                created_at=a.created_at,
                updated_at=a.updated_at,
            )

        def _artefact_version(v: Any) -> ArtefactVersionResponse:
            return ArtefactVersionResponse(
                artefact_id=v.artefact_id,
                version=v.version,
                content=v.content,
                summary=v.summary,
                created_at=v.created_at,
                created_by=v.created_by,
            )

        @app.get(
            "/sessions/{session_id}/artefacts",
            dependencies=[Depends(auth_dep)],
            response_model=list[ArtefactSummaryResponse],
        )
        async def list_artefacts(session_id: str) -> list[ArtefactSummaryResponse]:
            _ensure_session(session_id)
            artefacts = await artefact_store.list_for_session(session_id=session_id)
            return [_artefact_summary(a) for a in artefacts]

        @app.get(
            "/sessions/{session_id}/artefacts/{artefact_id}",
            dependencies=[Depends(auth_dep)],
            response_model=ArtefactReadResponse,
        )
        async def read_artefact(
            session_id: str,
            artefact_id: str,
            version: Optional[int] = None,
        ) -> ArtefactReadResponse:
            _ensure_session(session_id)
            try:
                a, v = await artefact_store.read(
                    artefact_id=artefact_id, version=version
                )
            except ArtefactNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e))
            if a.session_id != session_id:
                # Don't leak cross-session artefacts via path-guessing; pretend missing.
                raise HTTPException(status_code=404, detail="Artefact not found")
            return _artefact_read(a, v)

        @app.get(
            "/sessions/{session_id}/artefacts/{artefact_id}/versions",
            dependencies=[Depends(auth_dep)],
            response_model=list[ArtefactVersionResponse],
        )
        async def list_artefact_versions(
            session_id: str, artefact_id: str
        ) -> list[ArtefactVersionResponse]:
            _ensure_session(session_id)
            try:
                a = await artefact_store.get(artefact_id=artefact_id)
            except ArtefactNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e))
            if a.session_id != session_id:
                raise HTTPException(status_code=404, detail="Artefact not found")
            versions = await artefact_store.list_versions(artefact_id=artefact_id)
            return [_artefact_version(v) for v in versions]

        @app.get(
            "/sessions/{session_id}/artefacts/{artefact_id}/versions/{version}",
            dependencies=[Depends(auth_dep)],
            response_model=ArtefactVersionResponse,
        )
        async def read_artefact_version(
            session_id: str, artefact_id: str, version: int
        ) -> ArtefactVersionResponse:
            _ensure_session(session_id)
            try:
                a, v = await artefact_store.read(
                    artefact_id=artefact_id, version=version
                )
            except ArtefactNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e))
            if a.session_id != session_id:
                raise HTTPException(status_code=404, detail="Artefact not found")
            return _artefact_version(v)

    @app.get(
        "/sessions/{session_id}/snapshot",
        dependencies=[Depends(auth_dep)],
        response_model=SnapshotResponse,
    )
    async def get_snapshot(session_id: str) -> SnapshotResponse:
        s = _ensure_session(session_id)
        artefacts: list[ArtefactSummaryResponse] = []
        if artefact_store is not None:
            rows = await artefact_store.list_for_session(session_id=session_id)
            artefacts = [
                ArtefactSummaryResponse(
                    artefact_id=a.id,
                    session_id=a.session_id,
                    title=a.title,
                    kind=a.kind,
                    language=a.language,
                    current_version=a.current_version,
                    created_at=a.created_at,
                    updated_at=a.updated_at,
                )
                for a in rows
            ]
        return SnapshotResponse(
            session_id=session_id,
            protocol_version=PROTOCOL_VERSION,
            last_event_id=s.event_log.last_seq,
            messages=list(s.messages),
            artefacts=artefacts,
        )

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
