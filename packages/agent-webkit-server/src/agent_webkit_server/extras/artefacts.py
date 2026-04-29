"""Artefacts — persistent, session-scoped, versioned blobs the agent can author.

Artefacts pass the bridge-layer test:

* Stateful server-side: the store is the source of truth, surviving SSE-ring
  eviction and process restarts.
* Propagates to all subscribers: every mutation emits a wire event so concurrent
  subscribers stay in sync.
* Survives reconnect: the REST endpoints (mounted by ``create_app(artefact_store=...)``)
  let a client rehydrate from cold even after the event ring has rolled past.

The four tool surface the agent uses:

* ``create_artefact(title, kind, content, language?, summary?)``
* ``update_artefact(artefact_id, content, summary?)``
* ``read_artefact(artefact_id, version?)``
* ``list_artefacts()``

These are auto-allowed via :func:`wrap_can_use_tool_for_artefacts` because they
mutate only the bound store and emit only their own wire events; popping a
permission modal for "the agent wrote a draft" would defeat the feature.
"""
from __future__ import annotations

import abc
import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Literal, Optional

try:  # pragma: no cover — pydantic is a hard dep of agent-webkit-server
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    BaseModel = None  # type: ignore[assignment]


__all__ = [
    "Artefact",
    "ArtefactVersion",
    "ArtefactKind",
    "ArtefactStore",
    "InMemoryArtefactStore",
    "ArtefactNotFoundError",
    "ArtefactConflictError",
    "build_artefact_handlers",
    "build_artefact_mcp_server",
    "wrap_can_use_tool_for_artefacts",
    "ARTEFACT_TOOL_NAMES",
    "qualified_artefact_tool_names",
    "ARTEFACT_SERVER_NAME",
    "DEFAULT_SYSTEM_PROMPT",
]


ArtefactKind = Literal["text/markdown", "text/plain", "application/json", "text/code"]
_ALLOWED_KINDS: frozenset[str] = frozenset(
    {"text/markdown", "text/plain", "application/json", "text/code"}
)

ARTEFACT_SERVER_NAME = "artefacts"
ARTEFACT_TOOL_NAMES: tuple[str, ...] = (
    "create_artefact",
    "update_artefact",
    "read_artefact",
    "list_artefacts",
)


def qualified_artefact_tool_names() -> set[str]:
    """The wire-form (``mcp__artefacts__<tool>``) names of the four tools."""
    return {f"mcp__{ARTEFACT_SERVER_NAME}__{n}" for n in ARTEFACT_TOOL_NAMES} | set(
        ARTEFACT_TOOL_NAMES
    )


DEFAULT_SYSTEM_PROMPT = (
    "You can save longer outputs as **artefacts** — versioned documents the user "
    "sees in their UI side panel.\n"
    "- Use `create_artefact` when the user asks you to *draft, write, or maintain* "
    "a document, code file, or structured output that's longer than a couple of paragraphs.\n"
    "- Use `update_artefact` to edit an existing artefact; pass the full new content "
    "and a one-line `summary` of what changed.\n"
    "- Use `read_artefact` to retrieve a prior version when the user references it.\n"
    "- Use `list_artefacts` if you need to remember what's been authored.\n"
    "Do not store ephemeral chat or short answers as artefacts — these belong inline."
)


# ----- Domain models ---------------------------------------------------------

if BaseModel is not None:

    class Artefact(BaseModel):
        """Header / metadata for an artefact (no content)."""

        id: str
        session_id: str
        title: str
        kind: str
        language: Optional[str] = None
        current_version: int
        created_at: int  # epoch millis
        updated_at: int

    class ArtefactVersion(BaseModel):
        """One immutable version of an artefact."""

        artefact_id: str
        version: int
        content: str
        summary: Optional[str] = None
        created_at: int  # epoch millis
        created_by: Literal["agent", "user"] = "agent"

else:  # pragma: no cover — fallback if pydantic is absent

    @dataclass
    class Artefact:  # type: ignore[no-redef]
        id: str
        session_id: str
        title: str
        kind: str
        language: Optional[str]
        current_version: int
        created_at: int
        updated_at: int

    @dataclass
    class ArtefactVersion:  # type: ignore[no-redef]
        artefact_id: str
        version: int
        content: str
        summary: Optional[str]
        created_at: int
        created_by: str = "agent"


# ----- Errors ----------------------------------------------------------------


class ArtefactNotFoundError(KeyError):
    """Raised when ``artefact_id`` (or specific version) does not exist."""


class ArtefactConflictError(RuntimeError):
    """Raised on write contention or invariant violations."""


# ----- Store interface -------------------------------------------------------


class ArtefactStore(abc.ABC):
    """Abstract store. Implementations: :class:`InMemoryArtefactStore` (tests/dev)
    and :class:`agent_webkit_server.adapters.pg_artefact_store.PgArtefactStore`
    (production).

    Concurrency contract: ``create``/``update``/``delete`` for the same
    ``artefact_id`` must be serialised. Implementations are free to use any
    mechanism (process-local lock, advisory lock, row lock) — the in-memory
    impl uses an ``asyncio.Lock`` per id; Postgres uses
    ``pg_advisory_xact_lock``.
    """

    @abc.abstractmethod
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
        """Create a new artefact at version 1. Returns (Artefact, ArtefactVersion)."""

    @abc.abstractmethod
    async def update(
        self,
        *,
        artefact_id: str,
        content: str,
        summary: Optional[str] = None,
        created_by: str = "agent",
    ) -> tuple[Artefact, ArtefactVersion]:
        """Append a new version. Increments ``current_version`` atomically."""

    @abc.abstractmethod
    async def delete(self, *, artefact_id: str) -> None:
        """Soft-delete (sets ``deleted_at``). ``read`` then raises NotFound."""

    @abc.abstractmethod
    async def get(self, *, artefact_id: str) -> Artefact:
        """Header lookup by id. Raises :class:`ArtefactNotFoundError` if missing
        or soft-deleted."""

    @abc.abstractmethod
    async def read(
        self,
        *,
        artefact_id: str,
        version: Optional[int] = None,
    ) -> tuple[Artefact, ArtefactVersion]:
        """Return (header, version-row). ``version=None`` returns the current
        version. Raises :class:`ArtefactNotFoundError` for missing id or version."""

    @abc.abstractmethod
    async def list_versions(self, *, artefact_id: str) -> list[ArtefactVersion]:
        """All versions in ascending order."""

    @abc.abstractmethod
    async def list_for_session(self, *, session_id: str) -> list[Artefact]:
        """All non-deleted artefacts for this session, in creation order."""


# ----- In-memory implementation ---------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _validate_kind(kind: str) -> None:
    if kind not in _ALLOWED_KINDS:
        raise ValueError(
            f"Invalid artefact kind {kind!r}; allowed: "
            + ", ".join(sorted(_ALLOWED_KINDS))
        )


def _new_id() -> str:
    return f"art_{uuid.uuid4().hex[:24]}"


@dataclass
class _ArtefactRow:
    artefact: Artefact
    versions: list[ArtefactVersion] = field(default_factory=list)
    deleted: bool = False


class InMemoryArtefactStore(ArtefactStore):
    """Process-local artefact store. Useful for tests, single-process dev,
    and the Playwright/E2E reference server.

    The lock map intentionally accumulates one ``asyncio.Lock`` per
    ``artefact_id``. For long-running processes that's bounded by the artefact
    count of that process, which is small (these are user-visible documents,
    not log entries). If that ever ceases to be true we'd want a periodic
    purge of locks for soft-deleted artefacts; not needed today.
    """

    def __init__(self) -> None:
        self._rows: dict[str, _ArtefactRow] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def _lock_for(self, artefact_id: str) -> asyncio.Lock:
        lock = self._locks.get(artefact_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[artefact_id] = lock
        return lock

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

        async with self._global_lock:
            artefact_id = _new_id()
            now = _now_ms()
            artefact = Artefact(
                id=artefact_id,
                session_id=session_id,
                title=title,
                kind=kind,
                language=language,
                current_version=1,
                created_at=now,
                updated_at=now,
            )
            version = ArtefactVersion(
                artefact_id=artefact_id,
                version=1,
                content=content,
                summary=summary,
                created_at=now,
                created_by=created_by,  # type: ignore[arg-type]
            )
            self._rows[artefact_id] = _ArtefactRow(
                artefact=artefact,
                versions=[version],
                deleted=False,
            )
            return artefact, version

    async def update(
        self,
        *,
        artefact_id: str,
        content: str,
        summary: Optional[str] = None,
        created_by: str = "agent",
    ) -> tuple[Artefact, ArtefactVersion]:
        async with self._lock_for(artefact_id):
            row = self._rows.get(artefact_id)
            if row is None or row.deleted:
                raise ArtefactNotFoundError(artefact_id)
            now = _now_ms()
            next_v = row.artefact.current_version + 1
            new_artefact = row.artefact.model_copy(
                update={"current_version": next_v, "updated_at": now}
            ) if hasattr(row.artefact, "model_copy") else Artefact(
                **{**row.artefact.__dict__, "current_version": next_v, "updated_at": now}  # type: ignore[arg-type]
            )
            new_version = ArtefactVersion(
                artefact_id=artefact_id,
                version=next_v,
                content=content,
                summary=summary,
                created_at=now,
                created_by=created_by,  # type: ignore[arg-type]
            )
            row.artefact = new_artefact
            row.versions.append(new_version)
            return new_artefact, new_version

    async def delete(self, *, artefact_id: str) -> None:
        async with self._lock_for(artefact_id):
            row = self._rows.get(artefact_id)
            if row is None or row.deleted:
                raise ArtefactNotFoundError(artefact_id)
            row.deleted = True

    async def get(self, *, artefact_id: str) -> Artefact:
        row = self._rows.get(artefact_id)
        if row is None or row.deleted:
            raise ArtefactNotFoundError(artefact_id)
        return row.artefact

    async def read(
        self,
        *,
        artefact_id: str,
        version: Optional[int] = None,
    ) -> tuple[Artefact, ArtefactVersion]:
        row = self._rows.get(artefact_id)
        if row is None or row.deleted:
            raise ArtefactNotFoundError(artefact_id)
        if version is None:
            v = row.versions[-1]
        else:
            if version < 1 or version > len(row.versions):
                raise ArtefactNotFoundError(
                    f"version {version} of {artefact_id} does not exist"
                )
            v = row.versions[version - 1]
        return row.artefact, v

    async def list_versions(self, *, artefact_id: str) -> list[ArtefactVersion]:
        row = self._rows.get(artefact_id)
        if row is None or row.deleted:
            raise ArtefactNotFoundError(artefact_id)
        return list(row.versions)

    async def list_for_session(self, *, session_id: str) -> list[Artefact]:
        out = [
            r.artefact
            for r in self._rows.values()
            if not r.deleted and r.artefact.session_id == session_id
        ]
        out.sort(key=lambda a: a.created_at)
        return out


# ----- MCP server construction ----------------------------------------------


# Tool input schemas (JSON Schema). Hand-written so we don't bind to the SDK's
# pydantic version; matches the SDK's expected `MCP tool input schema` shape.
_CREATE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short human-readable title."},
        "kind": {
            "type": "string",
            "enum": sorted(_ALLOWED_KINDS),
            "description": "Content kind. text/markdown for prose, text/code for source.",
        },
        "content": {"type": "string", "description": "Full content body."},
        "language": {
            "type": "string",
            "description": "Optional, only meaningful when kind=text/code (e.g. 'python').",
        },
        "summary": {
            "type": "string",
            "description": "Optional one-line description of what this artefact is for.",
        },
    },
    "required": ["title", "kind", "content"],
}

_UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "artefact_id": {"type": "string"},
        "content": {"type": "string", "description": "Full new content (snapshot, not diff)."},
        "summary": {
            "type": "string",
            "description": "One-line description of what changed in this update.",
        },
    },
    "required": ["artefact_id", "content"],
}

_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "artefact_id": {"type": "string"},
        "version": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional. Defaults to the current version.",
        },
    },
    "required": ["artefact_id"],
}

_LIST_SCHEMA = {
    "type": "object",
    "properties": {},
}


EmitFn = Callable[[str, dict[str, Any]], Any]
"""Signature of ``EventLog.append`` — the wire-event emitter the handlers use."""


def _serialise_artefact(a: Artefact) -> dict[str, Any]:
    if hasattr(a, "model_dump"):
        return a.model_dump()  # type: ignore[no-any-return]
    return dict(a.__dict__)  # type: ignore[arg-type]


def _serialise_version(v: ArtefactVersion) -> dict[str, Any]:
    if hasattr(v, "model_dump"):
        return v.model_dump()  # type: ignore[no-any-return]
    return dict(v.__dict__)  # type: ignore[arg-type]


def build_artefact_handlers(
    *,
    store: ArtefactStore,
    session_id: str,
    emit: EmitFn,
) -> dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]]:
    """Construct the four artefact tool handlers without binding them to an
    MCP server. Useful for unit tests that want to exercise the handler
    bodies directly. :func:`build_artefact_mcp_server` builds on top of this.

    Each handler closes over (store, session_id, emit) and:

    1. Updates the persistent store (source of truth).
    2. Emits a wire event so all current subscribers see the change in the
       SSE log. Late subscribers that fall off the ring rehydrate via the REST
       endpoints — eviction-safe by construction.
    """

    async def _create(args: dict[str, Any]) -> dict[str, Any]:
        try:
            artefact, version = await store.create(
                session_id=session_id,
                title=args["title"],
                kind=args["kind"],
                content=args["content"],
                language=args.get("language"),
                summary=args.get("summary"),
            )
        except (ValueError, ArtefactConflictError) as e:
            return {
                "content": [{"type": "text", "text": f"error: {e}"}],
                "isError": True,
            }
        emit(
            "artefact_created",
            {
                "artefact_id": artefact.id,
                "title": artefact.title,
                "kind": artefact.kind,
                "language": artefact.language,
                "version": version.version,
                "content": version.content,
                "summary": version.summary,
                "session_id": artefact.session_id,
                "created_at": artefact.created_at,
            },
        )
        result = {"artefact_id": artefact.id, "version": version.version}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    async def _update(args: dict[str, Any]) -> dict[str, Any]:
        try:
            artefact, version = await store.update(
                artefact_id=args["artefact_id"],
                content=args["content"],
                summary=args.get("summary"),
            )
        except ArtefactNotFoundError as e:
            return {
                "content": [{"type": "text", "text": f"error: artefact not found ({e})"}],
                "isError": True,
            }
        emit(
            "artefact_updated",
            {
                "artefact_id": artefact.id,
                "version": version.version,
                "content": version.content,
                "summary": version.summary,
                "updated_at": artefact.updated_at,
            },
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"artefact_id": artefact.id, "version": version.version}
                    ),
                }
            ]
        }

    async def _read(args: dict[str, Any]) -> dict[str, Any]:
        try:
            artefact, version = await store.read(
                artefact_id=args["artefact_id"],
                version=args.get("version"),
            )
        except ArtefactNotFoundError as e:
            return {
                "content": [{"type": "text", "text": f"error: {e}"}],
                "isError": True,
            }
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "artefact_id": artefact.id,
                            "title": artefact.title,
                            "kind": artefact.kind,
                            "language": artefact.language,
                            "version": version.version,
                            "content": version.content,
                            "summary": version.summary,
                        }
                    ),
                }
            ]
        }

    async def _list(_args: dict[str, Any]) -> dict[str, Any]:
        artefacts = await store.list_for_session(session_id=session_id)
        rows = [
            {
                "artefact_id": a.id,
                "title": a.title,
                "kind": a.kind,
                "language": a.language,
                "current_version": a.current_version,
            }
            for a in artefacts
        ]
        return {"content": [{"type": "text", "text": json.dumps(rows)}]}

    return {
        "create_artefact": _create,
        "update_artefact": _update,
        "read_artefact": _read,
        "list_artefacts": _list,
    }


_TOOL_DESCRIPTIONS: dict[str, tuple[str, dict[str, Any]]] = {
    "create_artefact": (
        "Create a new artefact (versioned document) bound to this session.",
        _CREATE_SCHEMA,
    ),
    "update_artefact": (
        "Append a new version to an existing artefact. Pass the full new content.",
        _UPDATE_SCHEMA,
    ),
    "read_artefact": (
        "Read an artefact's content. Defaults to the current version.",
        _READ_SCHEMA,
    ),
    "list_artefacts": ("List artefacts in this session.", _LIST_SCHEMA),
}


def build_artefact_mcp_server(
    *,
    store: ArtefactStore,
    session_id: str,
    emit: EmitFn,
) -> Any:
    """Construct an in-process MCP server exposing the four artefact tools.

    The MCP server is per-session: tool handlers close over ``session_id`` so
    two concurrent sessions can't see each other's artefacts.
    """
    from claude_agent_sdk import create_sdk_mcp_server, tool  # type: ignore

    handlers = build_artefact_handlers(
        store=store, session_id=session_id, emit=emit
    )
    tools_list = [
        tool(name, desc, schema)(handlers[name])
        for name, (desc, schema) in _TOOL_DESCRIPTIONS.items()
    ]
    return create_sdk_mcp_server(ARTEFACT_SERVER_NAME, tools=tools_list)


# ----- can_use_tool wrapper --------------------------------------------------


def wrap_can_use_tool_for_artefacts(
    can_use_tool: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Auto-allow the four artefact tools.

    Artefact tools are scoped to the bound store and emit only their own wire
    events. Popping a permission modal would defeat the feature (and the SDK
    docs explicitly recommend auto-allow for in-process MCP tools the host
    application owns).
    """
    auto = qualified_artefact_tool_names()

    try:
        from claude_agent_sdk.types import PermissionResultAllow  # type: ignore
    except ImportError:  # pragma: no cover
        from ..sdk_bridge import PermissionResultAllow  # type: ignore

    async def wrapped(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        if tool_name in auto:
            return PermissionResultAllow()
        return await can_use_tool(tool_name, tool_input, context)

    return wrapped


def system_prompt_addendum() -> str:
    """The default system-prompt nudge attached when ``artefact_store=`` is
    passed to :func:`create_app`. Override at the ``create_app`` call site if
    you want a different nudge."""
    return DEFAULT_SYSTEM_PROMPT
