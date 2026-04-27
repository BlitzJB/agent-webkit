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
    InboundMessage,
)
from ..sdk_bridge import ConflictError, SDKClient
from ..session import BackpressureError, SessionConfig, SessionRegistry

logger = logging.getLogger(__name__)


def _make_real_sdk_factory(session_store: Any = None):  # pragma: no cover - requires real claude_agent_sdk; covered indirectly by integration tests
    """Build the default factory, optionally pre-wired with a SessionStore.

    The callback is plumbed in via ClaudeAgentOptions(can_use_tool=...) — the SDK's
    documented contract — so no post-construction mutation is required. If a
    ``session_store`` is supplied it is forwarded to ``ClaudeAgentOptions`` so the
    SDK persists conversation state through it.
    """
    async def factory(config: SessionConfig, can_use_tool: Any) -> SDKClient:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # type: ignore

        options_kwargs: dict[str, Any] = {"can_use_tool": can_use_tool}
        if config.model:
            options_kwargs["model"] = config.model
        if config.permission_mode:
            options_kwargs["permission_mode"] = config.permission_mode
        if config.cwd:
            options_kwargs["cwd"] = config.cwd
        if session_store is not None:
            options_kwargs["session_store"] = session_store

        options = ClaudeAgentOptions(**options_kwargs)
        client = ClaudeSDKClient(options=options)
        await client.connect()
        return client  # type: ignore[return-value]

    return factory


# Backwards-compatible default factory (no session_store).
_real_sdk_factory = _make_real_sdk_factory()


def create_app(
    *,
    auth: Optional[AuthConfig] = None,
    sdk_factory=None,
    session_store: Any = None,
) -> FastAPI:
    """Build a FastAPI app exposing the agent-webkit wire protocol.

    Args:
        auth: Bearer-token auth policy. Defaults to ``AuthConfig.from_env()``.
        sdk_factory: Optional callable building each session's ``ClaudeSDKClient``.
            Override to inject custom ``ClaudeAgentOptions`` (system prompt, MCP servers,
            hooks, etc.). When supplied, ``session_store`` is ignored — the caller is
            responsible for wiring it into their own factory.
        session_store: Optional ``SessionStore`` instance forwarded to
            ``ClaudeAgentOptions(session_store=...)`` by the default factory. Use this
            with the bundled :class:`PgSessionStore` for failover-friendly setups.
    """
    auth = auth or AuthConfig.from_env()
    if sdk_factory is None:
        sdk_factory = _make_real_sdk_factory(session_store=session_store)
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
    async def stream(session_id: str, request: Request) -> StreamingResponse:
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

        # Pre-flight: if the cursor is evicted, return 412 before opening the stream.
        if after_seq:
            try:
                # subscribe()'s constructor doesn't run; iterate one step to detect.
                gen_iter = s.event_log.subscribe(after_seq).__aiter__()
                # Try to advance once with a 0-timeout so we never block.
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
                sub = s.event_log.subscribe(after_seq).__aiter__()
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
