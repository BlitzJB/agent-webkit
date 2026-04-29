"""Companion FastAPI server for the genui-demo frontend.

Registers two GenUI components (WeatherCard, PricingTable) and mounts them
into the agent-webkit server. The agent will be nudged (via system-prompt
addendum) to call ``render_weather_card`` / ``render_pricing_table`` when
asked questions like "show me the weather in Boston" or "compare your plans".
"""
from __future__ import annotations

import argparse
import os
from typing import Optional

import uvicorn
from pydantic import BaseModel, Field

from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig
from agent_webkit_server.extras.genui import GenUIRegistry


class WeatherCard(BaseModel):
    """Display the current weather for a location."""

    location: str = Field(description="Human-readable place name, e.g. 'Boston, MA'")
    temperature_f: float
    condition: Optional[str] = Field(
        default=None, description="Short label, e.g. 'sunny', 'cloudy'"
    )


class PricingPlan(BaseModel):
    name: str
    price: float
    features: list[str] = Field(default_factory=list)


class PricingTable(BaseModel):
    """Show a side-by-side comparison of pricing plans."""

    plans: list[PricingPlan]


def build_registry() -> GenUIRegistry:
    reg = GenUIRegistry()
    reg.register(WeatherCard)
    reg.register(PricingTable)
    return reg


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-auth", action="store_true")
    p.add_argument("--token", default=os.environ.get("AGENT_WEBKIT_TOKEN"))
    args = p.parse_args()

    auth = AuthConfig(disabled=args.no_auth, token=args.token)
    app = create_app(auth=auth, genui=build_registry())
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
