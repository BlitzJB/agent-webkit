"""End-to-end: PgSessionStore wired into a real ClaudeSDKClient.

Proves the adapter participates in the SDK's actual persistence path — a real
Claude turn writes entries through the store, and a fresh client started with
``resume=<session_id>`` rehydrates from Postgres alone (no on-disk JSONL).

Skipped unless both a Postgres DSN and Claude credentials are configured.
"""
from __future__ import annotations

import asyncio
import os

import pytest


def _has_real_creds() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    )


pytestmark = pytest.mark.skipif(
    not _has_real_creds(),
    reason="No ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN set; skipping live SDK test",
)


@pytest.mark.asyncio
async def test_real_turn_persists_entries_to_postgres(fresh_pg_store) -> None:
    """A real Claude turn writes user+assistant entries through the PgSessionStore."""
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # type: ignore

    options = ClaudeAgentOptions(session_store=fresh_pg_store)
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        await client.query("Reply with exactly the word 'pong'.")

        async def drain_until_result() -> str | None:
            sid: str | None = None
            async for msg in client.receive_messages():
                # ResultMessage marks turn end across SDK versions.
                cls = type(msg).__name__
                if hasattr(msg, "session_id") and getattr(msg, "session_id"):
                    sid = msg.session_id  # type: ignore[attr-defined]
                if cls == "ResultMessage":
                    return sid
            return sid

        session_id = await asyncio.wait_for(drain_until_result(), timeout=60.0)
        assert session_id, "SDK did not surface a session_id"
    finally:
        await client.disconnect()

    # The SDK calls store.append() for the turn — entries must be in Postgres.
    sessions = [
        s for s in await fresh_pg_store.list_sessions(_default_project_key())
        if s["session_id"] == session_id
    ]
    # project_key defaults to sanitized cwd; if the SDK used a different key,
    # walk all keys to find ours.
    if not sessions:
        loaded = await _find_session_anywhere(fresh_pg_store, session_id)
    else:
        loaded = await fresh_pg_store.load(
            {"project_key": sessions[0].get("project_key", _default_project_key()),
             "session_id": session_id}
        )

    assert loaded, "No entries persisted to Postgres for the real session"
    types = {e.get("type") for e in loaded}
    # At minimum the user prompt must be there; assistant entries land too in practice.
    assert "user" in types, f"Expected user entry in transcript, got types={types}"


def _default_project_key() -> str:
    """Mirror the SDK's default: sanitized absolute cwd."""
    cwd = os.path.abspath(os.getcwd())
    return cwd.replace("/", "-").replace(os.sep, "-")


async def _find_session_anywhere(store, session_id: str):
    """Scan all project_keys for the session — the SDK's project_key derivation
    is internal, so we tolerate not knowing it exactly."""
    async with store._pool.acquire() as conn:  # type: ignore[attr-defined]
        row = await conn.fetchrow(
            "SELECT project_key FROM session_entries WHERE session_id = $1 LIMIT 1",
            session_id,
        )
    if not row:
        return None
    return await store.load({"project_key": row["project_key"], "session_id": session_id})


async def _drain_first_turn(client) -> str:
    """Drain a turn and return the session_id surfaced by the SDK."""
    sid: str | None = None
    async for msg in client.receive_messages():
        if hasattr(msg, "session_id") and getattr(msg, "session_id"):
            sid = msg.session_id  # type: ignore[attr-defined]
        if type(msg).__name__ == "ResultMessage":
            assert sid, "SDK did not surface a session_id"
            return sid
    raise AssertionError("Stream ended before ResultMessage")


@pytest.mark.asyncio
async def test_resume_from_pg_store_rehydrates_in_fresh_client(fresh_pg_store) -> None:
    """Cold-start replay: a fresh client started with ``resume=<session_id>``
    and the same store reads back the prior turn from Postgres alone.

    Establishes a session with a memorable token in turn 1, disconnects the
    client entirely, then opens a new client pointed at the same store with
    ``resume=<session_id>`` and asks a follow-up that requires the model to
    remember the token. If resume hydrates from the store, the model can
    answer; if it doesn't, it can't.
    """
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # type: ignore

    token = "ZQXP-7741"  # arbitrary, distinctive enough that the model won't guess

    # Turn 1: seed context + capture session_id.
    options1 = ClaudeAgentOptions(session_store=fresh_pg_store)
    client1 = ClaudeSDKClient(options=options1)
    await client1.connect()
    try:
        await client1.query(
            f"Remember this token exactly: {token}. "
            f"Reply with just the word 'ok' to confirm."
        )
        session_id = await asyncio.wait_for(_drain_first_turn(client1), timeout=60.0)
    finally:
        await client1.disconnect()

    # Sanity: the entries actually landed in Postgres before we test resume.
    loaded = await _find_session_anywhere(fresh_pg_store, session_id)
    assert loaded, "Turn 1 didn't persist; can't meaningfully test resume"

    # Turn 2: brand-new client, same store, resume=session_id.
    options2 = ClaudeAgentOptions(
        session_store=fresh_pg_store,
        resume=session_id,
    )
    client2 = ClaudeSDKClient(options=options2)
    await client2.connect()
    try:
        await client2.query(
            "What was the token I asked you to remember? Reply with just the token."
        )

        text_chunks: list[str] = []
        async for msg in client2.receive_messages():
            if type(msg).__name__ == "AssistantMessage":
                for blk in getattr(msg, "content", []) or []:
                    txt = getattr(blk, "text", None)
                    if txt:
                        text_chunks.append(txt)
            if type(msg).__name__ == "ResultMessage":
                break
    finally:
        await client2.disconnect()

    full = "".join(text_chunks)
    assert token in full, (
        f"Resumed client did not recall the token from store. "
        f"Got: {full!r}"
    )
