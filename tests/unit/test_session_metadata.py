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


@pytest.mark.asyncio
async def test_list_returns_all_saved_metadata(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    a = SessionMetadata(id=str(uuid.uuid4()), sdk_session_id="a", cwd="/work/a")
    b = SessionMetadata(id=str(uuid.uuid4()), sdk_session_id="b", cwd="/work/b")
    await store.save(a)
    await store.save(b)
    listed = await store.list()
    ids = sorted(m.id for m in listed)
    assert ids == sorted([a.id, b.id])


@pytest.mark.asyncio
async def test_list_skips_corrupt_files_without_raising(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    good = SessionMetadata(id=str(uuid.uuid4()))
    await store.save(good)
    # Drop a junk file alongside — list() must not raise.
    (tmp_path / f"{uuid.uuid4()}.json").write_text("not json")
    listed = await store.list()
    assert any(m.id == good.id for m in listed)


@pytest.mark.asyncio
async def test_list_ignores_tmp_files_from_in_flight_writes(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path)
    good = SessionMetadata(id=str(uuid.uuid4()))
    await store.save(good)
    # Simulate a tmp file left behind by a crashed write.
    (tmp_path / f"{uuid.uuid4()}.json.tmp.abc123").write_text("{}")
    listed = await store.list()
    assert [m.id for m in listed] == [good.id]


@pytest.mark.asyncio
async def test_list_empty_on_fresh_directory(tmp_path) -> None:
    store = FileSessionMetadataStore(tmp_path / "fresh")
    assert await store.list() == []
