"""Tests for create_app(artefact_store=...) — REST endpoints and snapshot wiring.

These tests don't run the real claude_agent_sdk; they substitute a no-op SDK
client so :class:`SessionRegistry.create` succeeds end-to-end and we can
exercise the artefact REST endpoints + ``/snapshot`` against a real
:class:`InMemoryArtefactStore`.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator

import pytest
from fastapi.testclient import TestClient

from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig
from agent_webkit_server.extras.artefacts import InMemoryArtefactStore


class _FakeSDKClient:
    """Minimal SDKClient that never produces messages.

    receive_messages() returns an iterator that yields nothing and waits forever
    on the next call — this lets the receive loop start without blocking the
    test, while the send loop sits idle (we never call /input in these tests).
    """

    async def connect(self, prompt: Any | None = None) -> None:  # noqa: D401
        return None

    async def query(self, prompt: Any) -> None:
        return None

    def receive_messages(self) -> AsyncIterator[Any]:
        async def _gen() -> AsyncIterator[Any]:
            # Park forever; the receive loop is happy to await indefinitely.
            await asyncio.Event().wait()
            yield  # pragma: no cover
        return _gen()

    async def interrupt(self) -> None:
        return None

    async def set_permission_mode(self, mode: str) -> None:
        return None

    async def set_model(self, model: str | None) -> None:
        return None

    async def stop_task(self, task_id: str) -> None:
        return None

    async def disconnect(self) -> None:
        return None


async def _fake_factory(config, can_use_tool, *, event_log_append=None):
    return _FakeSDKClient()


@pytest.fixture
def no_auth() -> AuthConfig:
    return AuthConfig(disabled=True)


@pytest.fixture
def app_with_artefacts(no_auth: AuthConfig):
    store = InMemoryArtefactStore()
    app = create_app(
        auth=no_auth,
        artefact_store=store,
        sdk_factory=_fake_factory,
    )
    return app, store


class TestRESTEndpointsRequireSession:
    def test_list_artefacts_404_for_unknown_session(
        self, app_with_artefacts
    ) -> None:
        app, _ = app_with_artefacts
        with TestClient(app) as client:
            res = client.get("/sessions/nope/artefacts")
        assert res.status_code == 404

    def test_snapshot_404_for_unknown_session(self, app_with_artefacts) -> None:
        app, _ = app_with_artefacts
        with TestClient(app) as client:
            res = client.get("/sessions/nope/snapshot")
        assert res.status_code == 404


class TestArtefactRESTEndpoints:
    def test_list_returns_session_scoped_summaries(
        self, app_with_artefacts
    ) -> None:
        app, store = app_with_artefacts
        with TestClient(app) as client:
            sid = client.post("/sessions", json={}).json()["session_id"]
            other_sid = client.post("/sessions", json={}).json()["session_id"]
            # Pre-populate via the store directly (not via tool calls).
            asyncio.run(
                store.create(
                    session_id=sid,
                    title="Plan",
                    kind="text/markdown",
                    content="hello",
                )
            )
            asyncio.run(
                store.create(
                    session_id=other_sid,
                    title="Other",
                    kind="text/markdown",
                    content="x",
                )
            )

            rows = client.get(f"/sessions/{sid}/artefacts").json()
            assert [r["title"] for r in rows] == ["Plan"]
            assert rows[0]["current_version"] == 1

    def test_read_returns_current_version_by_default(
        self, app_with_artefacts
    ) -> None:
        app, store = app_with_artefacts
        with TestClient(app) as client:
            sid = client.post("/sessions", json={}).json()["session_id"]
            a, _ = asyncio.run(
                store.create(
                    session_id=sid,
                    title="t",
                    kind="text/markdown",
                    content="v1",
                )
            )
            asyncio.run(store.update(artefact_id=a.id, content="v2"))

            res = client.get(f"/sessions/{sid}/artefacts/{a.id}")
            assert res.status_code == 200
            body = res.json()
            assert body["version"] == 2
            assert body["content"] == "v2"

    def test_read_specific_version(self, app_with_artefacts) -> None:
        app, store = app_with_artefacts
        with TestClient(app) as client:
            sid = client.post("/sessions", json={}).json()["session_id"]
            a, _ = asyncio.run(
                store.create(
                    session_id=sid,
                    title="t",
                    kind="text/markdown",
                    content="v1",
                )
            )
            asyncio.run(store.update(artefact_id=a.id, content="v2"))

            res = client.get(
                f"/sessions/{sid}/artefacts/{a.id}/versions/1"
            )
            assert res.status_code == 200
            assert res.json()["content"] == "v1"

            list_res = client.get(
                f"/sessions/{sid}/artefacts/{a.id}/versions"
            )
            assert [v["version"] for v in list_res.json()] == [1, 2]

    def test_read_does_not_leak_artefacts_across_sessions(
        self, app_with_artefacts
    ) -> None:
        app, store = app_with_artefacts
        with TestClient(app) as client:
            sid_a = client.post("/sessions", json={}).json()["session_id"]
            sid_b = client.post("/sessions", json={}).json()["session_id"]
            a, _ = asyncio.run(
                store.create(
                    session_id=sid_a,
                    title="A",
                    kind="text/markdown",
                    content="x",
                )
            )
            # Hitting session B's path with A's artefact id must 404.
            res = client.get(f"/sessions/{sid_b}/artefacts/{a.id}")
            assert res.status_code == 404


class TestSnapshot:
    def test_snapshot_returns_messages_and_artefacts(
        self, app_with_artefacts
    ) -> None:
        app, store = app_with_artefacts
        with TestClient(app) as client:
            sid = client.post("/sessions", json={}).json()["session_id"]
            asyncio.run(
                store.create(
                    session_id=sid,
                    title="P",
                    kind="text/markdown",
                    content="x",
                )
            )
            res = client.get(f"/sessions/{sid}/snapshot")
            assert res.status_code == 200
            body = res.json()
            assert body["session_id"] == sid
            assert body["protocol_version"] == "1.0"
            assert isinstance(body["last_event_id"], int)
            assert body["last_event_id"] >= 1  # session_ready emitted on start
            assert len(body["artefacts"]) == 1
            assert body["artefacts"][0]["title"] == "P"


class TestEndpointsNotMountedWithoutStore:
    def test_artefact_endpoints_404_when_no_store(
        self, no_auth: AuthConfig
    ) -> None:
        app = create_app(auth=no_auth, sdk_factory=_fake_factory)
        with TestClient(app) as client:
            sid = client.post("/sessions", json={}).json()["session_id"]
            res = client.get(f"/sessions/{sid}/artefacts")
            # Path not registered — FastAPI returns 404.
            assert res.status_code == 404

    def test_snapshot_still_mounted_without_store_and_returns_empty_artefacts(
        self, no_auth: AuthConfig
    ) -> None:
        app = create_app(auth=no_auth, sdk_factory=_fake_factory)
        with TestClient(app) as client:
            sid = client.post("/sessions", json={}).json()["session_id"]
            res = client.get(f"/sessions/{sid}/snapshot")
            assert res.status_code == 200
            assert res.json()["artefacts"] == []


class TestMessagesEndpoint:
    def test_messages_returns_user_message_after_submit(
        self, app_with_artefacts
    ) -> None:
        app, _ = app_with_artefacts
        with TestClient(app) as client:
            sid = client.post("/sessions", json={}).json()["session_id"]
            r = client.post(
                f"/sessions/{sid}/input",
                json={"type": "user_message", "content": "hello"},
            )
            assert r.status_code == 204
            res = client.get(f"/sessions/{sid}/messages")
            assert res.status_code == 200
            body = res.json()
            assert body["session_id"] == sid
            assert body["messages"] == [{"role": "user", "content": "hello"}]
