"""Tests for the per-session artefact MCP tool handlers.

We exercise the four handlers directly via :func:`build_artefact_handlers` so
we verify both the store mutation and the wire event emission without spinning
up the SDK transport machinery. :func:`build_artefact_mcp_server` is a thin
wrapper around that builder.
"""
from __future__ import annotations

import json

import pytest

from agent_webkit_server.extras.artefacts import (
    ARTEFACT_SERVER_NAME,
    ARTEFACT_TOOL_NAMES,
    InMemoryArtefactStore,
    build_artefact_handlers,
    build_artefact_mcp_server,
    qualified_artefact_tool_names,
    wrap_can_use_tool_for_artefacts,
)


def _captured_emit():
    out: list[tuple[str, dict]] = []

    def emit(event: str, data: dict) -> None:
        out.append((event, data))

    return emit, out


@pytest.mark.asyncio
async def test_create_handler_persists_and_emits_artefact_created() -> None:
    store = InMemoryArtefactStore()
    emit, captured = _captured_emit()
    h = build_artefact_handlers(store=store, session_id="s1", emit=emit)

    res = await h["create_artefact"](
        {"title": "Plan", "kind": "text/markdown", "content": "hello"}
    )
    body = json.loads(res["content"][0]["text"])
    assert "artefact_id" in body
    assert body["version"] == 1
    assert captured == [
        (
            "artefact_created",
            {
                **captured[0][1],
                "artefact_id": body["artefact_id"],
                "version": 1,
                "content": "hello",
                "session_id": "s1",
            },
        )
    ]
    # Store mirrors the change.
    rows = await store.list_for_session(session_id="s1")
    assert [a.title for a in rows] == ["Plan"]


@pytest.mark.asyncio
async def test_update_handler_emits_artefact_updated() -> None:
    store = InMemoryArtefactStore()
    emit, captured = _captured_emit()
    h = build_artefact_handlers(store=store, session_id="s1", emit=emit)

    create_res = await h["create_artefact"](
        {"title": "T", "kind": "text/markdown", "content": "v1"}
    )
    aid = json.loads(create_res["content"][0]["text"])["artefact_id"]

    upd = await h["update_artefact"](
        {"artefact_id": aid, "content": "v2", "summary": "bumped"}
    )
    body = json.loads(upd["content"][0]["text"])
    assert body["version"] == 2

    update_events = [e for e in captured if e[0] == "artefact_updated"]
    assert len(update_events) == 1
    assert update_events[0][1]["version"] == 2
    assert update_events[0][1]["content"] == "v2"
    assert update_events[0][1]["summary"] == "bumped"


@pytest.mark.asyncio
async def test_update_unknown_artefact_returns_isError() -> None:
    store = InMemoryArtefactStore()
    emit, _ = _captured_emit()
    h = build_artefact_handlers(store=store, session_id="s1", emit=emit)

    res = await h["update_artefact"](
        {"artefact_id": "art_does_not_exist", "content": "x"}
    )
    assert res.get("isError") is True


@pytest.mark.asyncio
async def test_read_handler_returns_payload_and_does_not_emit() -> None:
    store = InMemoryArtefactStore()
    emit, captured = _captured_emit()
    h = build_artefact_handlers(store=store, session_id="s1", emit=emit)

    create_res = await h["create_artefact"](
        {"title": "T", "kind": "text/markdown", "content": "v1"}
    )
    aid = json.loads(create_res["content"][0]["text"])["artefact_id"]

    captured.clear()
    read_res = await h["read_artefact"]({"artefact_id": aid})
    body = json.loads(read_res["content"][0]["text"])
    assert body["content"] == "v1"
    assert body["version"] == 1
    assert captured == []  # reads don't emit


@pytest.mark.asyncio
async def test_list_handler_scopes_to_session() -> None:
    store = InMemoryArtefactStore()
    emit, _ = _captured_emit()
    h_s1 = build_artefact_handlers(store=store, session_id="s1", emit=emit)
    h_s2 = build_artefact_handlers(store=store, session_id="s2", emit=emit)

    await h_s1["create_artefact"](
        {"title": "A", "kind": "text/markdown", "content": "x"}
    )
    await h_s2["create_artefact"](
        {"title": "B", "kind": "text/markdown", "content": "x"}
    )

    s1_list = json.loads((await h_s1["list_artefacts"]({}))["content"][0]["text"])
    assert [r["title"] for r in s1_list] == ["A"]


@pytest.mark.asyncio
async def test_create_validation_failure_does_not_emit() -> None:
    store = InMemoryArtefactStore()
    emit, captured = _captured_emit()
    h = build_artefact_handlers(store=store, session_id="s1", emit=emit)

    res = await h["create_artefact"](
        {"title": "Bad", "kind": "text/html", "content": "x"}
    )
    assert res.get("isError") is True
    assert captured == []


def test_qualified_tool_names_match_server_name() -> None:
    names = qualified_artefact_tool_names()
    for n in ARTEFACT_TOOL_NAMES:
        assert f"mcp__{ARTEFACT_SERVER_NAME}__{n}" in names
        assert n in names


def test_build_artefact_mcp_server_returns_sdk_server_dict() -> None:
    store = InMemoryArtefactStore()
    server = build_artefact_mcp_server(
        store=store, session_id="s1", emit=lambda *a, **kw: None
    )
    # SDK shape: {"type": "sdk", "name": ..., "instance": ...}
    assert isinstance(server, dict)
    assert server.get("type") == "sdk"
    assert server.get("name") == ARTEFACT_SERVER_NAME


@pytest.mark.asyncio
async def test_wrap_can_use_tool_auto_allows_qualified_artefact_tools() -> None:
    calls: list[str] = []

    async def underlying(tool_name, tool_input, ctx):
        calls.append(tool_name)
        return {"behavior": "deny"}

    wrapped = wrap_can_use_tool_for_artefacts(underlying)

    res = await wrapped(
        f"mcp__{ARTEFACT_SERVER_NAME}__create_artefact", {}, None
    )
    assert getattr(res, "behavior", "allow") == "allow"
    assert calls == []

    await wrapped("Bash", {}, None)
    assert calls == ["Bash"]
