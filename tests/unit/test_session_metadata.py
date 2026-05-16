"""FileSessionMetadataStore — round-trip + edge cases."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from agent_webkit_server.session_metadata import (
    FileSessionMetadataStore,
    SessionMetadata,
)


@pytest.mark.asyncio
async def test_save_then_load_roundtrips_all_fields(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    sid = str(uuid.uuid4())
    meta = SessionMetadata(
        id=sid,
        sdk_session_id="sdk-abc",
        model="claude-opus-4-7",
        permission_mode="acceptEdits",
        cwd="/work",
        include_partial_messages=True,
    )
    await store.save(meta)

    loaded = await store.load(sid)
    assert loaded is not None
    assert loaded.id == sid
    assert loaded.sdk_session_id == "sdk-abc"
    assert loaded.model == "claude-opus-4-7"
    assert loaded.permission_mode == "acceptEdits"
    assert loaded.cwd == "/work"
    assert loaded.include_partial_messages is True


@pytest.mark.asyncio
async def test_load_missing_returns_none(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    assert await store.load(str(uuid.uuid4())) is None


@pytest.mark.asyncio
async def test_delete_is_idempotent(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    sid = str(uuid.uuid4())
    await store.delete(sid)  # already missing
    await store.save(SessionMetadata(id=sid))
    await store.delete(sid)
    await store.delete(sid)  # gone now, must not raise
    assert await store.load(sid) is None


@pytest.mark.asyncio
async def test_save_is_atomic_under_concurrent_writes(tmp_path) -> None:
    """Two writes for the same id must not corrupt the file; the last writer
    wins. (We're not asserting which writer — only that the final file parses.)"""
    store = FileSessionMetadataStore(tmp_path)
    sid = str(uuid.uuid4())
    a = SessionMetadata(id=sid, sdk_session_id="a")
    b = SessionMetadata(id=sid, sdk_session_id="b")
    await asyncio.gather(store.save(a), store.save(b))
    loaded = await store.load(sid)
    assert loaded is not None
    assert loaded.sdk_session_id in {"a", "b"}


@pytest.mark.asyncio
async def test_load_corrupted_file_returns_none(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    sid = str(uuid.uuid4())
    (tmp_path / f"{sid}.json").write_text("not json")
    assert await store.load(sid) is None


@pytest.mark.asyncio
async def test_invalid_session_id_rejected(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    # Save with a bad id should refuse (path traversal guard).
    with pytest.raises(ValueError):
        await store.save(SessionMetadata(id="../escaped"))
    # Load/delete with a bad id silently return / no-op.
    assert await store.load("../escaped") is None
    await store.delete("../escaped")
