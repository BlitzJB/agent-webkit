"""SessionRegistry resume — survives in-memory loss via the metadata store.

When a session is removed from the in-memory map (uvicorn restart, idle
reap with purge_metadata=False), ``get_or_resume`` rebuilds it under the
*same* wrapper UUID with a fresh event_log/router/client, passing the
captured ``sdk_session_id`` to the factory so the SDK resumes the prior
transcript transparently.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_webkit_server.session import SessionConfig, SessionRegistry
from agent_webkit_server.session_metadata import (
    FileSessionMetadataStore,
    SessionMetadata,
)
from tests.fake_claude_sdk import FakeClaudeSDKClient
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def _factory_capturing(captured: list[SessionConfig], fixture: str):
    """Factory that records every config it was called with so we can assert
    `resume` gets threaded through on rebuild."""
    async def factory(config: SessionConfig, can_use_tool=None):
        captured.append(config)
        return FakeClaudeSDKClient(FIXTURES / f"{fixture}.jsonl", can_use_tool=can_use_tool)
    return factory


@pytest.mark.asyncio
async def test_create_persists_initial_metadata(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    captured: list[SessionConfig] = []
    registry = SessionRegistry(
        _factory_capturing(captured, "plain_qa"),
        metadata_store=store,
    )
    s = await registry.create(SessionConfig(model="claude-opus-4-7"))

    # Initial save happens immediately, before any ResultMessage.
    meta = await store.load(s.id)
    assert meta is not None
    assert meta.id == s.id
    assert meta.sdk_session_id is None  # SDK hasn't produced one yet
    assert meta.model == "claude-opus-4-7"
    await registry.shutdown()


@pytest.mark.asyncio
async def test_sdk_session_id_captured_from_result_and_persisted(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    captured: list[SessionConfig] = []
    registry = SessionRegistry(
        _factory_capturing(captured, "plain_qa"),
        metadata_store=store,
    )
    s = await registry.create(SessionConfig())

    # Drive a user message through; the fixture replies with an AssistantMessage
    # and a ResultMessage(session_id="fake-1"), which should be captured.
    await s.submit_user_message("hello")
    # Wait for the receive loop to drain the fixture.
    for _ in range(50):
        if s.sdk_session_id is not None:
            break
        await asyncio.sleep(0.02)

    assert s.sdk_session_id == "fake-1"
    # Give the persistence callback a tick to land.
    for _ in range(50):
        meta = await store.load(s.id)
        if meta and meta.sdk_session_id == "fake-1":
            break
        await asyncio.sleep(0.02)
    assert meta is not None
    assert meta.sdk_session_id == "fake-1"
    await registry.shutdown()


@pytest.mark.asyncio
async def test_get_or_resume_rebuilds_lost_session_with_resume_flag(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    captured: list[SessionConfig] = []
    registry = SessionRegistry(
        _factory_capturing(captured, "plain_qa"),
        metadata_store=store,
    )

    # Phase 1: create and drive to completion so sdk_session_id is captured.
    # cwd MUST round-trip through resume — the SDK looks up the transcript by
    # cwd-hash, so a different cwd on rebuild would fail to find it.
    s1 = await registry.create(SessionConfig(
        model="claude-opus-4-7",
        permission_mode="default",
        cwd="/work/repo-a",
    ))
    await s1.submit_user_message("hi")
    for _ in range(50):
        if s1.sdk_session_id is not None:
            break
        await asyncio.sleep(0.02)
    sid = s1.id
    assert s1.sdk_session_id == "fake-1"

    # Phase 2: simulate process loss without purging metadata (reaper path).
    await registry.remove(sid, purge_metadata=False)
    assert registry.get(sid) is None

    # Phase 3: rebuild (returns a shell — no factory call yet thanks to lazy spawn).
    s2 = await registry.get_or_resume(sid)
    assert s2 is not None
    assert s2.id == sid  # same wrapper UUID — clients don't have to know
    assert len(captured) == 1, "resume must NOT spawn a new SDK subprocess; lazy"

    # The first real interaction is what fires the spawn. After that the
    # factory has been called twice and the second call must carry the
    # original config plus resume=<sdk_session_id>.
    await s2.ensure_started()
    assert len(captured) == 2
    rebuild_cfg = captured[1]
    assert rebuild_cfg.resume == "fake-1"
    assert rebuild_cfg.model == "claude-opus-4-7"
    assert rebuild_cfg.permission_mode == "default"
    # Crucially: the cwd must match what was provided on the original create.
    # Diverging here breaks SDK transcript lookup and resume silently fails.
    assert rebuild_cfg.cwd == "/work/repo-a"
    await registry.shutdown()


@pytest.mark.asyncio
async def test_get_or_resume_returns_none_for_unknown_id(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    captured: list[SessionConfig] = []
    registry = SessionRegistry(
        _factory_capturing(captured, "plain_qa"),
        metadata_store=store,
    )
    assert await registry.get_or_resume("00000000-0000-0000-0000-000000000000") is None


@pytest.mark.asyncio
async def test_metadata_without_sdk_session_id_starts_fresh_under_same_wrapper(tmp_path) -> None:
    """Metadata-only entries (user created the session but refreshed before
    typing — no ResultMessage, no sdk_session_id) must still be resumable:
    spin up a fresh SDK client under the same wrapper id, no resume=.
    Refusing here would leave the user stranded with a 404 on a session id
    they're holding."""
    store = FileSessionMetadataStore(tmp_path)
    sid = "11111111-1111-1111-1111-111111111111"
    await store.save(SessionMetadata(
        id=sid, sdk_session_id=None, cwd="/work/x", model="claude-opus-4-7",
    ))

    captured: list[SessionConfig] = []
    registry = SessionRegistry(
        _factory_capturing(captured, "plain_qa"),
        metadata_store=store,
    )
    s = await registry.get_or_resume(sid)
    assert s is not None
    assert s.id == sid
    # Lazy spawn: get_or_resume does NOT invoke the factory; the first real
    # interaction does. Once we trigger ensure_started, the factory runs
    # with the original config and resume=None (fresh start, no transcript).
    assert captured == []
    await s.ensure_started()
    assert len(captured) == 1
    assert captured[0].resume is None
    assert captured[0].cwd == "/work/x"
    assert captured[0].model == "claude-opus-4-7"
    await registry.shutdown()


@pytest.mark.asyncio
async def test_explicit_remove_purges_metadata_by_default(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    captured: list[SessionConfig] = []
    registry = SessionRegistry(
        _factory_capturing(captured, "plain_qa"),
        metadata_store=store,
    )
    s = await registry.create(SessionConfig())
    await registry.remove(s.id)  # default: purge_metadata=True
    assert await store.load(s.id) is None
    await registry.shutdown()


@pytest.mark.asyncio
async def test_no_metadata_store_means_no_resume(tmp_path) -> None:
    """Backward-compat: without a metadata_store, get_or_resume behaves like
    get() — in-memory only, no persistence, no resume."""
    captured: list[SessionConfig] = []
    registry = SessionRegistry(_factory_capturing(captured, "plain_qa"))
    s = await registry.create(SessionConfig())
    sid = s.id
    await registry.remove(sid, purge_metadata=False)
    assert await registry.get_or_resume(sid) is None
    await registry.shutdown()
