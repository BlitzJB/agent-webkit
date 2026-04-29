"""Unit tests for InMemoryArtefactStore — correctness of versioning, kind
validation, soft-delete, and per-session listing."""
from __future__ import annotations

import asyncio

import pytest

from agent_webkit_server.extras.artefacts import (
    ArtefactNotFoundError,
    InMemoryArtefactStore,
)


@pytest.fixture
def store() -> InMemoryArtefactStore:
    return InMemoryArtefactStore()


class TestCreate:
    @pytest.mark.asyncio
    async def test_create_returns_v1_artefact_with_matching_version_row(
        self, store: InMemoryArtefactStore
    ) -> None:
        a, v = await store.create(
            session_id="s1", title="Plan", kind="text/markdown", content="hello"
        )
        assert a.session_id == "s1"
        assert a.title == "Plan"
        assert a.current_version == 1
        assert v.artefact_id == a.id
        assert v.version == 1
        assert v.content == "hello"
        assert v.created_by == "agent"

    @pytest.mark.asyncio
    async def test_create_rejects_empty_title(
        self, store: InMemoryArtefactStore
    ) -> None:
        with pytest.raises(ValueError):
            await store.create(
                session_id="s1", title="   ", kind="text/markdown", content="x"
            )

    @pytest.mark.asyncio
    async def test_create_rejects_unknown_kind(
        self, store: InMemoryArtefactStore
    ) -> None:
        with pytest.raises(ValueError):
            await store.create(
                session_id="s1", title="x", kind="text/html", content="x"
            )


class TestUpdate:
    @pytest.mark.asyncio
    async def test_update_increments_version_monotonically(
        self, store: InMemoryArtefactStore
    ) -> None:
        a, _ = await store.create(
            session_id="s1", title="t", kind="text/markdown", content="v1"
        )
        a2, v2 = await store.update(artefact_id=a.id, content="v2", summary="bumped")
        a3, v3 = await store.update(artefact_id=a.id, content="v3")
        assert (v2.version, v3.version) == (2, 3)
        assert a3.current_version == 3
        assert v2.summary == "bumped"

    @pytest.mark.asyncio
    async def test_update_missing_raises(
        self, store: InMemoryArtefactStore
    ) -> None:
        with pytest.raises(ArtefactNotFoundError):
            await store.update(artefact_id="art_doesnotexist", content="x")

    @pytest.mark.asyncio
    async def test_concurrent_updates_serialise_per_id(
        self, store: InMemoryArtefactStore
    ) -> None:
        a, _ = await store.create(
            session_id="s1", title="t", kind="text/markdown", content="v1"
        )
        # Fire 10 updates concurrently; per-id lock must produce monotonic versions.
        results = await asyncio.gather(
            *(store.update(artefact_id=a.id, content=f"v{i}") for i in range(2, 12))
        )
        versions = sorted(v.version for _, v in results)
        assert versions == list(range(2, 12))


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_then_read_raises(
        self, store: InMemoryArtefactStore
    ) -> None:
        a, _ = await store.create(
            session_id="s1", title="t", kind="text/markdown", content="x"
        )
        await store.delete(artefact_id=a.id)
        with pytest.raises(ArtefactNotFoundError):
            await store.read(artefact_id=a.id)
        with pytest.raises(ArtefactNotFoundError):
            await store.get(artefact_id=a.id)

    @pytest.mark.asyncio
    async def test_delete_twice_raises(
        self, store: InMemoryArtefactStore
    ) -> None:
        a, _ = await store.create(
            session_id="s1", title="t", kind="text/markdown", content="x"
        )
        await store.delete(artefact_id=a.id)
        with pytest.raises(ArtefactNotFoundError):
            await store.delete(artefact_id=a.id)


class TestRead:
    @pytest.mark.asyncio
    async def test_read_default_returns_current(
        self, store: InMemoryArtefactStore
    ) -> None:
        a, _ = await store.create(
            session_id="s1", title="t", kind="text/markdown", content="v1"
        )
        await store.update(artefact_id=a.id, content="v2")
        _, v = await store.read(artefact_id=a.id)
        assert v.version == 2
        assert v.content == "v2"

    @pytest.mark.asyncio
    async def test_read_specific_version(
        self, store: InMemoryArtefactStore
    ) -> None:
        a, _ = await store.create(
            session_id="s1", title="t", kind="text/markdown", content="v1"
        )
        await store.update(artefact_id=a.id, content="v2")
        _, v = await store.read(artefact_id=a.id, version=1)
        assert v.version == 1
        assert v.content == "v1"

    @pytest.mark.asyncio
    async def test_read_out_of_range_version_raises(
        self, store: InMemoryArtefactStore
    ) -> None:
        a, _ = await store.create(
            session_id="s1", title="t", kind="text/markdown", content="v1"
        )
        with pytest.raises(ArtefactNotFoundError):
            await store.read(artefact_id=a.id, version=99)


class TestListing:
    @pytest.mark.asyncio
    async def test_list_for_session_excludes_other_sessions_and_deleted(
        self, store: InMemoryArtefactStore
    ) -> None:
        a1, _ = await store.create(
            session_id="s1", title="A", kind="text/markdown", content="x"
        )
        await store.create(
            session_id="s2", title="B", kind="text/markdown", content="x"
        )
        a3, _ = await store.create(
            session_id="s1", title="C", kind="text/markdown", content="x"
        )
        await store.delete(artefact_id=a3.id)

        s1_rows = await store.list_for_session(session_id="s1")
        assert [r.id for r in s1_rows] == [a1.id]

    @pytest.mark.asyncio
    async def test_list_versions_returns_all_in_order(
        self, store: InMemoryArtefactStore
    ) -> None:
        a, _ = await store.create(
            session_id="s1", title="t", kind="text/markdown", content="v1"
        )
        await store.update(artefact_id=a.id, content="v2")
        await store.update(artefact_id=a.id, content="v3")
        versions = await store.list_versions(artefact_id=a.id)
        assert [v.version for v in versions] == [1, 2, 3]
