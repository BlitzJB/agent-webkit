"""Real-agent integration test for generative UI.

Registers a typed component with ``GenUIRegistry``, builds the FastAPI app
with ``create_app(genui=...)``, and asks the live model to invoke the
render tool. Asserts the wire ``tool_use`` event carries the qualified
MCP tool name and validated props — exactly what L1 ``GenUIStream``
matches against on the client.

Gated on real credentials.
"""
from __future__ import annotations

import asyncio
import os
from typing import Optional

import pytest
from pydantic import BaseModel


def _has_real_creds() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    )


pytestmark = pytest.mark.skipif(
    not _has_real_creds(),
    reason="No ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN set; skipping live SDK tests",
)


class WeatherCard(BaseModel):
    """Display the weather for a location."""

    location: str
    temperature_f: float
    condition: Optional[str] = None


@pytest.fixture
async def real_genui_session():
    from agent_webkit_server.adapters.fastapi import _make_real_sdk_factory
    from agent_webkit_server.extras.genui import GenUIRegistry
    from agent_webkit_server.session import SessionConfig, SessionRegistry

    reg = GenUIRegistry()
    reg.register(WeatherCard)

    factory = _make_real_sdk_factory(genui=reg)
    registry = SessionRegistry(factory)
    session = await registry.create(SessionConfig())
    try:
        yield session, reg
    finally:
        await registry.shutdown()


@pytest.mark.asyncio
async def test_real_agent_invokes_render_tool_with_validated_props(real_genui_session) -> None:
    session, reg = real_genui_session
    await session.submit_user_message(
        "Render a weather card for Boston, MA at 72°F with condition 'sunny'. "
        "You MUST call the render_weather_card tool to do this — do not reply with prose."
    )

    saw_tool_use: dict = {}

    async def drive() -> None:
        async for ev in session.event_log.subscribe(after_seq=0):
            if ev.event == "tool_use":
                if ev.data.get("tool_name", "").startswith("mcp__genui__render_"):
                    saw_tool_use.update(ev.data)
            if ev.event == "result":
                return

    await asyncio.wait_for(drive(), timeout=120.0)

    expected = reg.by_short_name("weather_card")
    assert expected is not None
    assert saw_tool_use, "agent never invoked the render tool"
    assert saw_tool_use["tool_name"] == expected.qualified_name
    inp = saw_tool_use.get("input") or {}
    # The tool input must satisfy the pydantic schema (server-side enforced)
    assert "location" in inp
    assert isinstance(inp["location"], str)
    assert "temperature_f" in inp
