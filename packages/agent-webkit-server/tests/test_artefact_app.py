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


class TestRESTEndpointsColdStart:
    """The artefact and snapshot endpoints are cold-start safe: they read
    straight from the persistent stores and don't require an in-memory
    Session. An unknown session id returns an empty list rather than 404 —
    artefact_store is the source of truth, not the live registry."""

    def test_list_artefacts_for_unknown_session_returns_empty_not_404(
        self, app_with_artefacts
    ) -> None:
        app, _ = app_with_artefacts
        with TestClient(app) as client:
            res = client.get("/sessions/nope/artefacts")
        assert res.status_code == 200
        assert res.json() == []

    def test_snapshot_404_for_session_with_no_persistent_state(
        self, app_with_artefacts
    ) -> None:
        # /snapshot still 404s when nothing on the system knows about the id
        # — registry, session_store, and artefact_store all empty for this id.
        app, _ = app_with_artefacts
        with TestClient(app) as client:
            res = client.get("/sessions/never-existed/snapshot")
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


class _FakeSessionStore:
    """Minimal SessionStore stub for cold-start tests.

    Stores a list of SDK transcript entries per (project_key, session_id)
    and serves load(). project_key defaults to ``default`` to match
    create_app's default."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str, str], list[dict]] = {}

    def put(
        self,
        session_id: str,
        entries: list[dict],
        *,
        project_key: str = "default",
        subpath: str = "",
    ) -> None:
        self.data[(project_key, session_id, subpath)] = list(entries)

    async def load(self, key: dict):
        return self.data.get(
            (key["project_key"], key["session_id"], key.get("subpath") or "")
        )

    async def append(self, key: dict, entries: list[dict]) -> None:
        bucket = self.data.setdefault(
            (key["project_key"], key["session_id"], key.get("subpath") or ""),
            [],
        )
        bucket.extend(entries)


class TestColdStartReadThrough:
    def test_messages_endpoint_reads_from_session_store_when_no_live_session(
        self, no_auth: AuthConfig
    ) -> None:
        store = _FakeSessionStore()
        store.put(
            "cold-sid",
            [
                {"type": "user", "message": {"role": "user", "content": "hi"}},
                {
                    "type": "assistant",
                    "message": {
                        "id": "m1",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "hello"}],
                    },
                },
                {"type": "title", "value": "ignored"},
            ],
        )
        app = create_app(
            auth=no_auth,
            sdk_factory=_fake_factory,
            session_store=store,
        )
        with TestClient(app) as client:
            res = client.get("/sessions/cold-sid/messages")
        assert res.status_code == 200
        body = res.json()
        # User row projected, assistant row passes through, title dropped.
        assert body["messages"] == [
            {"role": "user", "content": "hi"},
            {
                "id": "m1",
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
            },
        ]
        assert body["session_id"] == "cold-sid"

    def test_snapshot_finds_known_session_via_session_store(
        self, no_auth: AuthConfig
    ) -> None:
        store = _FakeSessionStore()
        store.put(
            "cold-sid",
            [{"type": "user", "message": {"role": "user", "content": "x"}}],
        )
        app = create_app(
            auth=no_auth,
            sdk_factory=_fake_factory,
            session_store=store,
        )
        with TestClient(app) as client:
            res = client.get("/sessions/cold-sid/snapshot")
        assert res.status_code == 200
        body = res.json()
        assert body["session_id"] == "cold-sid"
        assert body["messages"] == [{"role": "user", "content": "x"}]

    def test_snapshot_finds_known_session_via_artefact_store_alone(
        self, no_auth: AuthConfig
    ) -> None:
        # No session_store, only artefact_store with rows for this id.
        astore = InMemoryArtefactStore()
        asyncio.run(
            astore.create(
                session_id="cold-sid",
                title="P",
                kind="text/markdown",
                content="hi",
            )
        )
        app = create_app(
            auth=no_auth,
            sdk_factory=_fake_factory,
            artefact_store=astore,
        )
        with TestClient(app) as client:
            res = client.get("/sessions/cold-sid/snapshot")
        assert res.status_code == 200
        body = res.json()
        # No messages (no session_store, no live session) but artefact present.
        assert body["messages"] == []
        assert [a["title"] for a in body["artefacts"]] == ["P"]


class TestColdStartSessionRehydration:
    def test_get_or_load_reinstantiates_session_when_store_has_it(
        self, no_auth: AuthConfig
    ) -> None:
        store = _FakeSessionStore()
        store.put(
            "ghost-sid",
            [{"type": "user", "message": {"role": "user", "content": "ping"}}],
        )
        captured_resume: list[str | None] = []

        async def factory(config, can_use_tool, *, event_log_append=None):
            captured_resume.append(config.resume)
            return _FakeSDKClient()

        app = create_app(
            auth=no_auth,
            sdk_factory=factory,
            session_store=store,
        )
        with TestClient(app) as client:
            # Streaming a never-created-this-process session must not 404 if
            # the store has the transcript — registry.get_or_load resumes it.
            res = client.get(
                "/sessions/ghost-sid/stream",
                headers={"accept": "text/event-stream"},
                timeout=0.5,
            )
            # SSE responses 200 even when nothing arrives within the test
            # window — we mainly care that the registry didn't 404 and that
            # the factory was called with resume=ghost-sid.
            assert res.status_code == 200
        assert captured_resume == ["ghost-sid"]


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
