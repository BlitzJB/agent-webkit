"""Postgres-backed :class:`ArtefactStore`.

Persistent companion to :class:`InMemoryArtefactStore`. Schema is created on
``connect()``; ``IF NOT EXISTS`` makes that idempotent.

Concurrency: every mutation grabs ``pg_advisory_xact_lock(hash(artefact_id))``
to serialise concurrent updates and keep ``current_version`` monotonically
correct without an optimistic-retry loop. Reads do not lock.

This adapter shares the optional-asyncpg dependency story with
:class:`PgSessionStore` — install with ``pip install asyncpg``.
"""
from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

from ..extras.artefacts import (
    Artefact,
    ArtefactNotFoundError,
    ArtefactStore,
    ArtefactVersion,
    _validate_kind,
    _new_id,
)

if TYPE_CHECKING:  # pragma: no cover - typing-only
    import asyncpg


def _import_asyncpg() -> Any:
    try:
        import asyncpg  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise ImportError(
            "PgArtefactStore requires the 'asyncpg' package. "
            "Install with: pip install asyncpg"
        ) from e
    return asyncpg


_DDL = """
CREATE TABLE IF NOT EXISTS artefacts (
    id              TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    title           TEXT NOT NULL,
    kind            TEXT NOT NULL,
    language        TEXT,
    current_version INT  NOT NULL,
    created_at_ms   BIGINT NOT NULL,
    updated_at_ms   BIGINT NOT NULL,
    deleted_at_ms   BIGINT
);
CREATE INDEX IF NOT EXISTS artefacts_session_idx
    ON artefacts (session_id) WHERE deleted_at_ms IS NULL;

CREATE TABLE IF NOT EXISTS artefact_versions (
    artefact_id  TEXT NOT NULL REFERENCES artefacts(id) ON DELETE CASCADE,
    version      INT  NOT NULL,
    content      TEXT NOT NULL,
    summary      TEXT,
    created_at_ms BIGINT NOT NULL,
    created_by   TEXT NOT NULL,
    PRIMARY KEY (artefact_id, version)
);
"""


def _lock_key(artefact_id: str) -> int:
    h = hash(f"art\x00{artefact_id}") & ((1 << 63) - 1)
    return h


def _row_to_artefact(row: Any) -> Artefact:
    return Artefact(
        id=row["id"],
        session_id=row["session_id"],
        title=row["title"],
        kind=row["kind"],
        language=row["language"],
        current_version=row["current_version"],
        created_at=int(row["created_at_ms"]),
        updated_at=int(row["updated_at_ms"]),
    )


def _row_to_version(row: Any) -> ArtefactVersion:
    return ArtefactVersion(
        artefact_id=row["artefact_id"],
        version=row["version"],
        content=row["content"],
        summary=row["summary"],
        created_at=int(row["created_at_ms"]),
        created_by=row["created_by"],
    )


class PgArtefactStore(ArtefactStore):
    def __init__(self, pool: "asyncpg.Pool") -> None:
        self._pool = pool

    @classmethod
    async def connect(
        cls,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
    ) -> "PgArtefactStore":
        asyncpg = _import_asyncpg()
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        store = cls(pool)
        await store._init_schema()
        return store

    @classmethod
    async def from_pool(cls, pool: "asyncpg.Pool") -> "PgArtefactStore":
        store = cls(pool)
        await store._init_schema()
        return store

    async def close(self) -> None:
        await self._pool.close()

    async def _init_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)

    async def create(
        self,
        *,
        session_id: str,
        title: str,
        kind: str,
        content: str,
        language: Optional[str] = None,
        summary: Optional[str] = None,
        created_by: str = "agent",
    ) -> tuple[Artefact, ArtefactVersion]:
        if not title.strip():
            raise ValueError("title must be non-empty")
        _validate_kind(kind)
        artefact_id = _new_id()
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Always-fresh ULID-ish id; collision odds are vanishingly low,
                # but advisory-lock anyway so the (artefact, version=1) pair is
                # written atomically against any concurrent writer.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)", _lock_key(artefact_id)
                )
                now = await conn.fetchval(
                    "SELECT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint"
                )
                await conn.execute(
                    """
                    INSERT INTO artefacts
                        (id, session_id, title, kind, language, current_version,
                         created_at_ms, updated_at_ms)
                    VALUES ($1, $2, $3, $4, $5, 1, $6, $6)
                    """,
                    artefact_id,
                    session_id,
                    title,
                    kind,
                    language,
                    now,
                )
                await conn.execute(
                    """
                    INSERT INTO artefact_versions
                        (artefact_id, version, content, summary, created_at_ms, created_by)
                    VALUES ($1, 1, $2, $3, $4, $5)
                    """,
                    artefact_id,
                    content,
                    summary,
                    now,
                    created_by,
                )
        # We could RETURNING-clause this, but the indirection keeps the row→model
        # mapping in one place. Two trips, both cheap.
        return await self.read(artefact_id=artefact_id)

    async def update(
        self,
        *,
        artefact_id: str,
        content: str,
        summary: Optional[str] = None,
        created_by: str = "agent",
    ) -> tuple[Artefact, ArtefactVersion]:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)", _lock_key(artefact_id)
                )
                row = await conn.fetchrow(
                    """
                    SELECT current_version, deleted_at_ms FROM artefacts WHERE id = $1
                    """,
                    artefact_id,
                )
                if row is None or row["deleted_at_ms"] is not None:
                    raise ArtefactNotFoundError(artefact_id)
                next_v = int(row["current_version"]) + 1
                now = await conn.fetchval(
                    "SELECT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint"
                )
                await conn.execute(
                    """
                    INSERT INTO artefact_versions
                        (artefact_id, version, content, summary, created_at_ms, created_by)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    artefact_id,
                    next_v,
                    content,
                    summary,
                    now,
                    created_by,
                )
                await conn.execute(
                    """
                    UPDATE artefacts
                    SET current_version = $2, updated_at_ms = $3
                    WHERE id = $1
                    """,
                    artefact_id,
                    next_v,
                    now,
                )
        return await self.read(artefact_id=artefact_id)

    async def delete(self, *, artefact_id: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)", _lock_key(artefact_id)
                )
                now = await conn.fetchval(
                    "SELECT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint"
                )
                result = await conn.execute(
                    """
                    UPDATE artefacts
                    SET deleted_at_ms = $2
                    WHERE id = $1 AND deleted_at_ms IS NULL
                    """,
                    artefact_id,
                    now,
                )
                # asyncpg returns "UPDATE 1" / "UPDATE 0".
                if not result.endswith(" 1"):
                    raise ArtefactNotFoundError(artefact_id)

    async def get(self, *, artefact_id: str) -> Artefact:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, session_id, title, kind, language, current_version,
                       created_at_ms, updated_at_ms
                FROM artefacts
                WHERE id = $1 AND deleted_at_ms IS NULL
                """,
                artefact_id,
            )
        if row is None:
            raise ArtefactNotFoundError(artefact_id)
        return _row_to_artefact(row)

    async def read(
        self,
        *,
        artefact_id: str,
        version: Optional[int] = None,
    ) -> tuple[Artefact, ArtefactVersion]:
        async with self._pool.acquire() as conn:
            arow = await conn.fetchrow(
                """
                SELECT id, session_id, title, kind, language, current_version,
                       created_at_ms, updated_at_ms
                FROM artefacts
                WHERE id = $1 AND deleted_at_ms IS NULL
                """,
                artefact_id,
            )
            if arow is None:
                raise ArtefactNotFoundError(artefact_id)
            target_version = version if version is not None else int(arow["current_version"])
            vrow = await conn.fetchrow(
                """
                SELECT artefact_id, version, content, summary, created_at_ms, created_by
                FROM artefact_versions
                WHERE artefact_id = $1 AND version = $2
                """,
                artefact_id,
                target_version,
            )
        if vrow is None:
            raise ArtefactNotFoundError(
                f"version {target_version} of {artefact_id} does not exist"
            )
        return _row_to_artefact(arow), _row_to_version(vrow)

    async def list_versions(self, *, artefact_id: str) -> list[ArtefactVersion]:
        async with self._pool.acquire() as conn:
            arow = await conn.fetchrow(
                "SELECT 1 FROM artefacts WHERE id = $1 AND deleted_at_ms IS NULL",
                artefact_id,
            )
            if arow is None:
                raise ArtefactNotFoundError(artefact_id)
            rows = await conn.fetch(
                """
                SELECT artefact_id, version, content, summary, created_at_ms, created_by
                FROM artefact_versions
                WHERE artefact_id = $1
                ORDER BY version ASC
                """,
                artefact_id,
            )
        return [_row_to_version(r) for r in rows]

    async def list_for_session(self, *, session_id: str) -> list[Artefact]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, title, kind, language, current_version,
                       created_at_ms, updated_at_ms
                FROM artefacts
                WHERE session_id = $1 AND deleted_at_ms IS NULL
                ORDER BY created_at_ms ASC
                """,
                session_id,
            )
        return [_row_to_artefact(r) for r in rows]
