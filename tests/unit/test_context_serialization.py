"""`_coerce_context` must return a JSON-serializable dict.

The SDK's `ToolPermissionContext.suggestions` is `list[PermissionUpdate]` —
nested dataclasses that `json.dumps` rejects. When that hits the wire as
part of a `permission_request` event the SSE response 500s mid-stream, the
client auto-reconnects, the server replays the same poisoned event from
the ring buffer, and the loop never ends.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from agent_webkit_server.sdk_bridge import _coerce_context


# Shape-equivalent fakes for ToolPermissionContext + PermissionUpdate so we
# can exercise the coercion path without importing the real SDK.
@dataclass
class _FakeRuleValue:
    tool_name: str
    rule_content: Optional[str] = None


@dataclass
class _FakePermissionUpdate:
    type: str
    mode: Optional[str] = None
    behavior: Optional[str] = None
    rules: Optional[list[_FakeRuleValue]] = None
    directories: Optional[list[str]] = None
    destination: Optional[str] = None


@dataclass
class _FakeToolPermissionContext:
    tool_use_id: Optional[str] = None
    correlation_id: Optional[str] = None
    agent_id: Optional[str] = None
    suggestions: Optional[list[_FakePermissionUpdate]] = None


def test_coerce_context_with_permission_update_suggestions_is_json_serializable() -> None:
    ctx = _FakeToolPermissionContext(
        tool_use_id="tu_1",
        suggestions=[
            _FakePermissionUpdate(
                type="addRules",
                behavior="allow",
                rules=[_FakeRuleValue(tool_name="Read", rule_content="/tmp/**")],
                destination="session",
            ),
            _FakePermissionUpdate(type="setMode", mode="acceptEdits"),
        ],
    )
    out = _coerce_context(ctx)
    # The whole payload must round-trip through json without raising.
    json.dumps(out)
    assert out["tool_use_id"] == "tu_1"
    assert isinstance(out["suggestions"], list)
    assert out["suggestions"][0]["type"] == "addRules"
    assert out["suggestions"][0]["behavior"] == "allow"
    assert out["suggestions"][0]["destination"] == "session"
    # Nested dataclasses (rules → _FakeRuleValue) must also flatten.
    assert out["suggestions"][0]["rules"] == [
        {"tool_name": "Read", "rule_content": "/tmp/**"}
    ]
    # asdict preserves explicit None fields — that's fine, JSON handles them.
    assert out["suggestions"][1]["type"] == "setMode"
    assert out["suggestions"][1]["mode"] == "acceptEdits"


def test_coerce_context_dict_passthrough_stays_json_serializable() -> None:
    """When the caller already provided a plain dict (test fixtures do), we
    must still return a JSON-serializable value — not silently re-wrap or
    drop fields."""
    ctx: dict[str, Any] = {
        "tool_use_id": "tu_x",
        "suggestions": [{"type": "addRules", "behavior": "deny"}],
    }
    out = _coerce_context(ctx)
    json.dumps(out)
    assert out == ctx


def test_coerce_context_handles_none_suggestions() -> None:
    ctx = _FakeToolPermissionContext(tool_use_id="tu_2", suggestions=None)
    out = _coerce_context(ctx)
    json.dumps(out)
    assert out == {"tool_use_id": "tu_2"}
