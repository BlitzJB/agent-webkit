"""Tests for create_app(genui=...) wiring.

We only assert on the HTTP-level behaviour: the schema endpoint is mounted
when a registry is supplied, returns the registry payload, and is not mounted
otherwise. The SDK-side wiring is covered by unit tests on the registry and
by the real-agent integration test (gated on CLAUDE_CODE_OAUTH_TOKEN).
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig
from agent_webkit_server.extras.genui import GenUIRegistry


class WeatherCard(BaseModel):
    """Show weather."""

    location: str


@pytest.fixture
def no_auth() -> AuthConfig:
    # Disable bearer-token auth for these tests.
    os.environ.pop("AGENT_WEBKIT_BEARER", None)
    return AuthConfig.from_env()


def test_genui_schema_endpoint_serves_registered_components(no_auth: AuthConfig) -> None:
    reg = GenUIRegistry()
    reg.register(WeatherCard)
    app = create_app(auth=no_auth, genui=reg, sdk_factory=lambda *a, **kw: None)
    client = TestClient(app)

    res = client.get("/genui/schema")
    assert res.status_code == 200
    body = res.json()
    assert body["server_name"] == "genui"
    assert body["prefix"] == "render_"
    names = {t["short_name"] for t in body["tools"]}
    assert names == {"weather_card"}


def test_genui_schema_endpoint_not_mounted_without_registry(no_auth: AuthConfig) -> None:
    app = create_app(auth=no_auth, sdk_factory=lambda *a, **kw: None)
    client = TestClient(app)
    res = client.get("/genui/schema")
    assert res.status_code == 404


def test_genui_schema_is_publicly_readable_even_with_auth() -> None:
    # The schema is a client-side contract, not session state — it must be readable
    # without auth so unauthenticated frontends can hydrate their renderer registry.
    os.environ["AGENT_WEBKIT_BEARER"] = "secret-token"
    try:
        reg = GenUIRegistry()
        reg.register(WeatherCard)
        app = create_app(auth=AuthConfig.from_env(), genui=reg, sdk_factory=lambda *a, **kw: None)
        client = TestClient(app)
        res = client.get("/genui/schema")
        assert res.status_code == 200
        # And the protected endpoints still 401:
        res2 = client.post("/sessions", json={})
        assert res2.status_code in (401, 403)
    finally:
        os.environ.pop("AGENT_WEBKIT_BEARER", None)
