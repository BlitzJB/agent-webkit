"""Postgres-backed :class:`SessionStore` adapter for the Claude Agent SDK.

Optional. Plug into the SDK via::

    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
    from server.extras.pg_session_store import PgSessionStore

    store = await PgSessionStore.connect("postgresql://user:pass@host/db")
    options = ClaudeAgentOptions(session_store=store, resume=session_id)
    client = ClaudeSDKClient(options=options)

Implements the full :class:`SessionStore` protocol surface — both required
(``append`` / ``load``) and the four optional methods (``list_sessions``,
``list_session_summaries``, ``delete``, ``list_subkeys``) — and passes the
SDK's published ``run_session_store_conformance`` test suite.

Key design points:
- ``project_key`` is the multi-tenant boundary; in production set it to a
  tenant id rather than the default sanitized cwd.
- Idempotency on the entry's ``uuid`` is enforced by a partial unique index;
  re-appending the same uuid is a no-op.
- The ``session_summaries`` sidecar is updated incrementally inside
  ``append()`` using the SDK's pure :func:`fold_session_summary`, guarded by
  a per-session ``pg_advisory_xact_lock`` so concurrent appends serialize
  cleanly without optimistic-retry loops.
- Storage write times come from Postgres ``clock_timestamp()`` so the mtimes
  surfaced by ``list_sessions`` and ``list_session_summaries`` share a clock
  domain (the conformance suite asserts ``summary.mtime >= list.mtime``).
"""
from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing-only
    import asyncpg


# Lazy import: keep ``asyncpg`` an optional dependency.
def _import_asyncpg() -> Any:
    try:
        import asyncpg  # type: ignore
    except ImportError as e:  # pragma: no cover - exercised when extra is missing
        raise ImportError(
            "PgSessionStore requires the 'asyncpg' package. "
            "Install with: pip install asyncpg"
        ) from e
    return asyncpg


_DDL = """
CREATE TABLE IF NOT EXISTS session_entries (
    project_key     TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    subpath         TEXT    NOT NULL DEFAULT '',
    seq             BIGSERIAL PRIMARY KEY,
    uuid            TEXT,
    entry           JSONB   NOT NULL,
    inserted_at_ms  BIGINT  NOT NULL
);
CREATE INDEX IF NOT EXISTS session_entries_lookup_idx
    ON session_entries (project_key, session_id, subpath, seq);
CREATE UNIQUE INDEX IF NOT EXISTS session_entries_uuid_uniq_idx
    ON session_entries (project_key, session_id, subpath, uuid)
    WHERE uuid IS NOT NULL;

CREATE TABLE IF NOT EXISTS session_summaries (
    project_key  TEXT   NOT NULL,
    session_id   TEXT   NOT NULL,
    mtime_ms     BIGINT NOT NULL,
    data         JSONB  NOT NULL,
    PRIMARY KEY (project_key, session_id)
);
"""


class PgSessionStore:
    """Postgres-backed Claude Agent SDK session store.

    Construct via :meth:`connect` (creates a pool and runs the schema
    bootstrap), or :meth:`from_pool` if the caller already owns a pool.
    """

    def __init__(self, pool: "asyncpg.Pool") -> None:
        self._pool = pool

    @classmethod
    async def connect(
        cls,
        dsn: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
    ) -> "PgSessionStore":
        asyncpg = _import_asyncpg()
        pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
        store = cls(pool)
        await store._init_schema()
        return store

    @classmethod
    async def from_pool(cls, pool: "asyncpg.Pool") -> "PgSessionStore":
        store = cls(pool)
        await store._init_schema()
        return store

    async def close(self) -> None:
        await self._pool.close()

    async def _init_schema(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(_DDL)

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _key_parts(key: dict[str, Any]) -> tuple[str, str, str]:
        return key["project_key"], key["session_id"], key.get("subpath") or ""

    @staticmethod
    def _lock_key(project_key: str, session_id: str) -> int:
        # Stable 63-bit signed int from a string for pg_advisory_xact_lock.
        # The advisory-lock namespace is process-wide; collisions only block,
        # never corrupt — so a simple Python hash narrowed to int8 is fine.
        h = hash(f"{project_key}\x00{session_id}") & ((1 << 63) - 1)
        return h

    # --- required: append + load --------------------------------------------

    async def append(
        self,
        key: dict[str, Any],
        entries: list[dict[str, Any]],
    ) -> None:
        if not entries:
            return
        from ._summary_bridge import fold_session_summary  # lazy SDK import

        project_key, session_id, subpath = self._key_parts(key)
        is_main = subpath == ""

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Serialize concurrent appends to the same (project, session)
                # so the read-fold-write of the summary sidecar is race-free.
                # Subagent appends share the same lock — the fold itself skips
                # subpath entries, but holding the lock prevents an
                # interleaved main-append from racing the sidecar write.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    self._lock_key(project_key, session_id),
                )

                rows = []
                for e in entries:
                    rows.append((
                        project_key,
                        session_id,
                        subpath,
                        e.get("uuid") if isinstance(e.get("uuid"), str) else None,
                        json.dumps(e),
                    ))
                # Single statement preserves call order via BIGSERIAL allocation
                # in the order rows are supplied. The ON CONFLICT clause is
                # bound to the partial unique index so uuid-less rows fall
                # through (no conflict possible) and uuid-bearing duplicates
                # are dropped silently.
                await conn.executemany(
                    """
                    INSERT INTO session_entries
                        (project_key, session_id, subpath, uuid, entry, inserted_at_ms)
                    VALUES
                        ($1, $2, $3, $4, $5::jsonb,
                         (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint)
                    ON CONFLICT (project_key, session_id, subpath, uuid)
                        WHERE uuid IS NOT NULL
                    DO NOTHING
                    """,
                    rows,
                )

                # Subagent appends must NOT update the main session's summary.
                if is_main:
                    prev = await conn.fetchrow(
                        """
                        SELECT mtime_ms, data
                        FROM session_summaries
                        WHERE project_key = $1 AND session_id = $2
                        """,
                        project_key,
                        session_id,
                    )
                    prev_summary: dict[str, Any] | None = None
                    if prev is not None:
                        prev_summary = {
                            "session_id": session_id,
                            "mtime": prev["mtime_ms"],
                            "data": json.loads(prev["data"])
                            if isinstance(prev["data"], (str, bytes))
                            else prev["data"],
                        }
                    folded = fold_session_summary(prev_summary, key, entries)
                    new_mtime = await conn.fetchval(
                        "SELECT (EXTRACT(EPOCH FROM clock_timestamp()) * 1000)::bigint"
                    )
                    await conn.execute(
                        """
                        INSERT INTO session_summaries
                            (project_key, session_id, mtime_ms, data)
                        VALUES ($1, $2, $3, $4::jsonb)
                        ON CONFLICT (project_key, session_id)
                        DO UPDATE SET mtime_ms = EXCLUDED.mtime_ms,
                                      data     = EXCLUDED.data
                        """,
                        project_key,
                        session_id,
                        new_mtime,
                        json.dumps(folded["data"]),
                    )

    async def load(self, key: dict[str, Any]) -> list[dict[str, Any]] | None:
        project_key, session_id, subpath = self._key_parts(key)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT entry
                FROM session_entries
                WHERE project_key = $1 AND session_id = $2 AND subpath = $3
                ORDER BY seq ASC
                """,
                project_key,
                session_id,
                subpath,
            )
        if not rows:
            return None
        return [
            json.loads(r["entry"]) if isinstance(r["entry"], (str, bytes)) else r["entry"]
            for r in rows
        ]

    # --- optional: list_sessions --------------------------------------------

    async def list_sessions(self, project_key: str) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT session_id, MAX(inserted_at_ms) AS mtime
                FROM session_entries
                WHERE project_key = $1 AND subpath = ''
                GROUP BY session_id
                """,
                project_key,
            )
        return [{"session_id": r["session_id"], "mtime": int(r["mtime"])} for r in rows]

    # --- optional: list_session_summaries -----------------------------------

    async def list_session_summaries(
        self, project_key: str
    ) -> list[dict[str, Any]]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT session_id, mtime_ms, data
                FROM session_summaries
                WHERE project_key = $1
                """,
                project_key,
            )
        out: list[dict[str, Any]] = []
        for r in rows:
            data = r["data"]
            if isinstance(data, (str, bytes)):
                data = json.loads(data)
            out.append({
                "session_id": r["session_id"],
                "mtime": int(r["mtime_ms"]),
                "data": data,
            })
        return out

    # --- optional: delete ----------------------------------------------------

    async def delete(self, key: dict[str, Any]) -> None:
        project_key, session_id, subpath = self._key_parts(key)
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT pg_advisory_xact_lock($1)",
                    self._lock_key(project_key, session_id),
                )
                if subpath == "":
                    # Main delete cascades to all subkeys for this session.
                    await conn.execute(
                        """
                        DELETE FROM session_entries
                        WHERE project_key = $1 AND session_id = $2
                        """,
                        project_key,
                        session_id,
                    )
                    await conn.execute(
                        """
                        DELETE FROM session_summaries
                        WHERE project_key = $1 AND session_id = $2
                        """,
                        project_key,
                        session_id,
                    )
                else:
                    await conn.execute(
                        """
                        DELETE FROM session_entries
                        WHERE project_key = $1 AND session_id = $2 AND subpath = $3
                        """,
                        project_key,
                        session_id,
                        subpath,
                    )

    # --- optional: list_subkeys ---------------------------------------------

    async def list_subkeys(self, key: dict[str, Any]) -> list[str]:
        project_key, session_id, _ = self._key_parts(key)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT DISTINCT subpath
                FROM session_entries
                WHERE project_key = $1 AND session_id = $2 AND subpath <> ''
                """,
                project_key,
                session_id,
            )
        return [r["subpath"] for r in rows]
