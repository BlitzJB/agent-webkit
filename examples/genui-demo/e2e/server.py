"""Boot the reference server with the fake SDK + GenUI registry for E2E tests.

The fake SDK replays ``fixtures/genui_render.jsonl``, which emits a
``tool_use`` for ``mcp__genui__render_weather_card`` regardless of the user's
input. The frontend hook dispatches that to the WeatherCard React renderer.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

import uvicorn
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from agent_webkit_server.adapters.fastapi import create_app  # noqa: E402
from agent_webkit_server.auth import AuthConfig  # noqa: E402
from agent_webkit_server.extras.genui import (  # noqa: E402
    GenUIRegistry,
    wrap_can_use_tool_for_genui,
)
from agent_webkit_server.session import SessionConfig  # noqa: E402
from tests.fake_claude_sdk import FakeClaudeSDKClient  # noqa: E402

FIXTURE = ROOT / "fixtures" / "genui_render.jsonl"


class WeatherCard(BaseModel):
    """Show weather."""

    location: str
    temperature_f: float
    condition: Optional[str] = None


class PricingPlan(BaseModel):
    name: str
    price: float
    features: list[str] = []


class PricingTable(BaseModel):
    """Compare pricing plans."""

    plans: list[PricingPlan]


registry = GenUIRegistry()
registry.register(WeatherCard)
registry.register(PricingTable)


async def factory(_config: SessionConfig, can_use_tool: Any = None) -> Any:
    wrapped = (
        wrap_can_use_tool_for_genui(can_use_tool, registry) if can_use_tool else None
    )
    return FakeClaudeSDKClient(FIXTURE, can_use_tool=wrapped)


def main() -> None:
    app = create_app(auth=AuthConfig(disabled=True), sdk_factory=factory, genui=registry)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
