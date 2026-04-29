"""Generative UI registry — typed components → SDK tools → schema endpoint.

The agent emits a ``tool_use`` event when it wants to "render" a registered
component. The tool name encodes which component, the tool input encodes the
props. The actual rendering happens client-side: the agent-webkit JS SDK
matches incoming ``tool_use`` events against the schema served at
``GET /genui/schema`` and dispatches to a renderer the user registered in their
frontend.

This module is pydantic-only (registry + schema synthesis). The MCP server
construction lives behind a lazy import of ``claude_agent_sdk`` so the registry
is testable without the SDK installed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Iterator, Optional

try:  # pragma: no cover — covered indirectly by tests with pydantic available
    from pydantic import BaseModel
except ImportError:  # pragma: no cover
    BaseModel = None  # type: ignore[assignment]


__all__ = [
    "GenUIEntry",
    "GenUIRegistry",
    "wrap_can_use_tool_for_genui",
]


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_DEFAULT_SYSTEM_PROMPT = (
    "You can render rich UI to the user by calling generative-UI tools. "
    "When you want to display structured information (a card, table, chart, "
    "summary, etc.) to the user, prefer calling the appropriate `{prefix}<name>` "
    "tool over plain prose. The component's input schema is the props the UI "
    "will receive. Do not call these tools to compute things — they exist purely "
    "for rendering, and you may freely call several in one turn."
)


@dataclass(frozen=True)
class GenUIEntry:
    """One registered component."""

    short_name: str
    """User-facing name (e.g. ``"weather_card"``). The L2 hook keys renderers by this."""

    raw_tool_name: str
    """The MCP tool's own name (e.g. ``"render_weather_card"``)."""

    qualified_name: str
    """Fully-qualified wire name as the SDK will emit it (e.g.
    ``"mcp__genui__render_weather_card"``). Match incoming ``tool_use`` events on this."""

    description: str
    schema: dict[str, Any]
    """JSON Schema produced by ``model.model_json_schema()``."""


def _camel_to_snake(name: str) -> str:
    s1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


class GenUIRegistry:
    """Registry of typed UI components.

    Args:
        server_name: MCP server name. Forms part of the qualified tool name as
            ``mcp__<server_name>__<raw_tool_name>``. Default ``"genui"``.
        prefix: Tool-name prefix. Default ``"render_"``. Set to ``""`` if your
            tool names already encode the action.
        system_prompt: Override the default system-prompt addendum that nudges
            the model to invoke render tools when appropriate. ``None`` (default)
            uses the bundled prompt; ``""`` disables the nudge entirely.

    Example:
        >>> from pydantic import BaseModel
        >>> class WeatherCard(BaseModel):
        ...     location: str
        ...     temperature_f: float
        >>> reg = GenUIRegistry()
        >>> reg.register(WeatherCard, description="Show a weather summary")
        >>> app = create_app(genui=reg)
    """

    def __init__(
        self,
        *,
        server_name: str = "genui",
        prefix: str = "render_",
        system_prompt: Optional[str] = None,
    ) -> None:
        if BaseModel is None:
            raise RuntimeError(
                "GenUIRegistry requires pydantic. "
                "Install with: pip install agent-webkit-server[genui]"
            )
        if not _NAME_RE.match(server_name):
            raise ValueError(
                f"Invalid server_name {server_name!r}; must match {_NAME_RE.pattern}"
            )
        self._server_name = server_name
        self._prefix = prefix
        self._system_prompt = system_prompt
        self._entries: dict[str, GenUIEntry] = {}

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def prefix(self) -> str:
        return self._prefix

    def register(
        self,
        model: type,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> GenUIEntry:
        """Register a Pydantic model as a renderable component.

        Returns the resulting ``GenUIEntry`` so callers can introspect the
        synthesized tool names if useful.
        """
        if BaseModel is None or not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError(
                "GenUIRegistry.register requires a pydantic.BaseModel subclass"
            )
        short = name if name is not None else _camel_to_snake(model.__name__)
        if not _NAME_RE.match(short):
            raise ValueError(
                f"Invalid component name {short!r}; must match {_NAME_RE.pattern}"
            )
        if short in self._entries:
            raise ValueError(f"Component {short!r} already registered")
        raw = f"{self._prefix}{short}"
        qualified = f"mcp__{self._server_name}__{raw}"
        if description is None:
            doc = (model.__doc__ or "").strip()
            description = doc or f"Render the {short} component"
        schema = model.model_json_schema()
        entry = GenUIEntry(
            short_name=short,
            raw_tool_name=raw,
            qualified_name=qualified,
            description=description,
            schema=schema,
        )
        self._entries[short] = entry
        return entry

    def __contains__(self, short_name: object) -> bool:
        return short_name in self._entries

    def __iter__(self) -> Iterator[GenUIEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def by_short_name(self, short: str) -> Optional[GenUIEntry]:
        return self._entries.get(short)

    def by_qualified_name(self, qualified: str) -> Optional[GenUIEntry]:
        for e in self._entries.values():
            if e.qualified_name == qualified:
                return e
        return None

    def auto_allowed_tool_names(self) -> set[str]:
        """Tool names a wrapped ``can_use_tool`` should allow without prompting."""
        names = {e.qualified_name for e in self._entries.values()}
        # Belt-and-braces: also accept the unqualified raw name in case any code
        # path strips the ``mcp__`` prefix before consulting can_use_tool.
        names |= {e.raw_tool_name for e in self._entries.values()}
        return names

    def schema_payload(self) -> dict[str, Any]:
        """Return the JSON body for ``GET /genui/schema``."""
        return {
            "version": "1.0",
            "server_name": self._server_name,
            "prefix": self._prefix,
            "tools": [
                {
                    "name": e.qualified_name,
                    "short_name": e.short_name,
                    "raw_tool_name": e.raw_tool_name,
                    "description": e.description,
                    "schema": e.schema,
                }
                for e in self._entries.values()
            ],
        }

    def allowed_tool_patterns(self) -> list[str]:
        """The list of qualified tool names to grant via ``allowed_tools``."""
        return [e.qualified_name for e in self._entries.values()]

    def system_prompt_addendum(self) -> str:
        """Generated system-prompt text that nudges the agent to use render tools.

        Returns an empty string if no components are registered or if the user
        explicitly passed ``system_prompt=""`` to the registry.
        """
        if self._system_prompt is not None:
            return self._system_prompt
        if not self._entries:
            return ""
        bullets = "\n".join(
            f"- `{e.raw_tool_name}` — {e.description}" for e in self._entries.values()
        )
        return (
            _DEFAULT_SYSTEM_PROMPT.format(prefix=self._prefix)
            + "\n\nAvailable components:\n"
            + bullets
        )

    def build_mcp_server(self) -> Any:
        """Build the in-process MCP server config to plumb into ``ClaudeAgentOptions``.

        Each registered component becomes one tool whose handler returns a stub
        ``"rendered: <name>"`` text result. The real UI work happens client-side
        — the tool exists purely so the agent can emit a ``tool_use`` event with
        validated props.
        """
        from claude_agent_sdk import create_sdk_mcp_server, tool  # type: ignore

        tools_list = []
        for e in self._entries.values():
            tools_list.append(
                tool(e.raw_tool_name, e.description, e.schema)(_make_handler(e))
            )
        return create_sdk_mcp_server(self._server_name, tools=tools_list)


def _make_handler(entry: GenUIEntry) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]:
    async def handler(_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "content": [
                {"type": "text", "text": f"rendered:{entry.short_name}"}
            ]
        }

    return handler


def wrap_can_use_tool_for_genui(
    can_use_tool: Callable[..., Awaitable[Any]],
    registry: GenUIRegistry,
) -> Callable[..., Awaitable[Any]]:
    """Wrap a ``can_use_tool`` callback so registered GenUI tool calls are auto-allowed.

    GenUI tools are render-only and have no side effects, so popping a permission
    modal for them would defeat their purpose. Anything not in ``registry`` falls
    through to the original callback unchanged.
    """
    auto = registry.auto_allowed_tool_names()

    try:
        from claude_agent_sdk.types import PermissionResultAllow  # type: ignore
    except ImportError:  # pragma: no cover — test/environment without the real SDK
        from ..sdk_bridge import PermissionResultAllow  # type: ignore

    async def wrapped(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        if tool_name in auto:
            return PermissionResultAllow()
        return await can_use_tool(tool_name, tool_input, context)

    return wrapped
