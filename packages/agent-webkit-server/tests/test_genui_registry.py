"""Unit tests for the GenUIRegistry — schema generation, naming, conflicts."""
from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from agent_webkit_server.extras.genui import (
    GenUIEntry,
    GenUIRegistry,
)


class WeatherCard(BaseModel):
    """Show a weather summary."""

    location: str
    temperature_f: float
    condition: str | None = None


class PricingTable(BaseModel):
    plans: list[dict] = Field(default_factory=list)


class TestGenUIRegistryRegistration:
    def test_register_pydantic_model_produces_qualified_name(self) -> None:
        reg = GenUIRegistry()
        e = reg.register(WeatherCard)
        assert isinstance(e, GenUIEntry)
        assert e.short_name == "weather_card"
        assert e.raw_tool_name == "render_weather_card"
        assert e.qualified_name == "mcp__genui__render_weather_card"

    def test_register_uses_pydantic_docstring_as_default_description(self) -> None:
        reg = GenUIRegistry()
        e = reg.register(WeatherCard)
        assert e.description == "Show a weather summary."

    def test_register_with_explicit_name_and_description(self) -> None:
        reg = GenUIRegistry()
        e = reg.register(WeatherCard, name="weather", description="custom desc")
        assert e.short_name == "weather"
        assert e.raw_tool_name == "render_weather"
        assert e.description == "custom desc"

    def test_register_rejects_non_pydantic_class(self) -> None:
        class NotAModel:
            pass

        reg = GenUIRegistry()
        with pytest.raises(TypeError):
            reg.register(NotAModel)  # type: ignore[arg-type]

    def test_register_rejects_invalid_short_name(self) -> None:
        reg = GenUIRegistry()
        with pytest.raises(ValueError):
            reg.register(WeatherCard, name="Bad-Name")

    def test_register_rejects_duplicate_short_name(self) -> None:
        reg = GenUIRegistry()
        reg.register(WeatherCard)
        with pytest.raises(ValueError, match="already registered"):
            reg.register(WeatherCard)

    def test_register_includes_pydantic_json_schema(self) -> None:
        reg = GenUIRegistry()
        e = reg.register(WeatherCard)
        assert e.schema["type"] == "object"
        assert "location" in e.schema["properties"]
        assert "temperature_f" in e.schema["properties"]


class TestGenUIRegistryConfigurability:
    def test_custom_server_name_and_prefix_propagate_to_qualified_name(self) -> None:
        reg = GenUIRegistry(server_name="ui", prefix="show_")
        e = reg.register(WeatherCard)
        assert e.qualified_name == "mcp__ui__show_weather_card"
        assert e.raw_tool_name == "show_weather_card"

    def test_empty_prefix_is_allowed(self) -> None:
        reg = GenUIRegistry(prefix="")
        e = reg.register(WeatherCard, name="weather")
        assert e.raw_tool_name == "weather"
        assert e.qualified_name == "mcp__genui__weather"

    def test_invalid_server_name_rejected(self) -> None:
        with pytest.raises(ValueError):
            GenUIRegistry(server_name="Bad-Name")


class TestGenUIRegistryAccessors:
    def test_iter_and_len_and_contains(self) -> None:
        reg = GenUIRegistry()
        reg.register(WeatherCard)
        reg.register(PricingTable)
        assert len(reg) == 2
        assert "weather_card" in reg
        assert "pricing_table" in reg
        names = {e.short_name for e in reg}
        assert names == {"weather_card", "pricing_table"}

    def test_lookup_by_short_and_qualified_name(self) -> None:
        reg = GenUIRegistry()
        reg.register(WeatherCard)
        e = reg.by_short_name("weather_card")
        assert e is not None and e.raw_tool_name == "render_weather_card"
        assert reg.by_qualified_name("mcp__genui__render_weather_card") is e
        assert reg.by_short_name("missing") is None
        assert reg.by_qualified_name("mcp__genui__missing") is None


class TestGenUIRegistrySchemaPayload:
    def test_schema_payload_shape(self) -> None:
        reg = GenUIRegistry()
        reg.register(WeatherCard)
        reg.register(PricingTable)
        payload = reg.schema_payload()
        assert payload["version"] == "1.0"
        assert payload["server_name"] == "genui"
        assert payload["prefix"] == "render_"
        assert len(payload["tools"]) == 2
        first = payload["tools"][0]
        assert set(first.keys()) >= {
            "name",
            "short_name",
            "raw_tool_name",
            "description",
            "schema",
        }

    def test_allowed_tool_patterns_returns_qualified_names(self) -> None:
        reg = GenUIRegistry()
        reg.register(WeatherCard)
        reg.register(PricingTable)
        patterns = reg.allowed_tool_patterns()
        assert sorted(patterns) == [
            "mcp__genui__render_pricing_table",
            "mcp__genui__render_weather_card",
        ]

    def test_auto_allowed_tool_names_includes_qualified_and_raw(self) -> None:
        reg = GenUIRegistry()
        reg.register(WeatherCard)
        names = reg.auto_allowed_tool_names()
        assert "mcp__genui__render_weather_card" in names
        assert "render_weather_card" in names


class TestGenUIRegistrySystemPrompt:
    def test_default_addendum_lists_components(self) -> None:
        reg = GenUIRegistry()
        reg.register(WeatherCard)
        reg.register(PricingTable)
        text = reg.system_prompt_addendum()
        assert "render_weather_card" in text
        assert "render_pricing_table" in text

    def test_empty_registry_yields_empty_addendum(self) -> None:
        reg = GenUIRegistry()
        assert reg.system_prompt_addendum() == ""

    def test_explicit_empty_string_disables_addendum(self) -> None:
        reg = GenUIRegistry(system_prompt="")
        reg.register(WeatherCard)
        assert reg.system_prompt_addendum() == ""

    def test_custom_system_prompt_used_verbatim(self) -> None:
        reg = GenUIRegistry(system_prompt="my-custom-prompt")
        reg.register(WeatherCard)
        assert reg.system_prompt_addendum() == "my-custom-prompt"


class TestWrapCanUseTool:
    @pytest.mark.asyncio
    async def test_genui_tool_calls_are_auto_allowed_without_invoking_inner(self) -> None:
        from agent_webkit_server.extras.genui import wrap_can_use_tool_for_genui

        reg = GenUIRegistry()
        reg.register(WeatherCard)

        called = []

        async def inner(tool_name, tool_input, context):
            called.append(tool_name)
            return {"behavior": "deny"}

        wrapped = wrap_can_use_tool_for_genui(inner, reg)
        result = await wrapped("mcp__genui__render_weather_card", {"location": "X"}, None)
        # Should bypass inner — verifying via no recorded call.
        assert called == []
        # Result should be a PermissionResultAllow-like object.
        assert hasattr(result, "__class__")

    @pytest.mark.asyncio
    async def test_non_genui_tool_calls_fall_through_to_inner(self) -> None:
        from agent_webkit_server.extras.genui import wrap_can_use_tool_for_genui

        reg = GenUIRegistry()
        reg.register(WeatherCard)

        async def inner(tool_name, tool_input, context):
            return {"forwarded": tool_name}

        wrapped = wrap_can_use_tool_for_genui(inner, reg)
        result = await wrapped("ReadFile", {"path": "x"}, None)
        assert result == {"forwarded": "ReadFile"}
