"""PermissionUpdate must round-trip through the wire cleanly.

When the client clicks an "Always allow" suggestion chip, it sends the
suggestion back to the server as a dict inside `updated_permissions`. The
bridge must hydrate that dict into a real `PermissionUpdate` instance before
handing it to `PermissionResultAllow` — otherwise the SDK eventually calls
`.to_dict()` on a plain dict and raises:

    'dict' object has no attribute 'to_dict'

We also verify the outbound shape: suggestions emitted on the
`permission_request` wire event must use `PermissionUpdate.to_dict()` so the
rules carry the camelCase keys (`toolName`, `ruleContent`) the SDK's
`from_dict` expects on the way back.
"""
from __future__ import annotations

from typing import Any

import pytest

from agent_webkit_server.sdk_bridge import (
    build_can_use_tool,
    PermissionRouter,
    _coerce_context,
)


@pytest.fixture
def real_sdk_required() -> Any:
    """Skip if the real claude_agent_sdk isn't importable — these tests are
    asserting against the SDK's actual PermissionUpdate dataclass."""
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk.types import PermissionUpdate, PermissionRuleValue
    return PermissionUpdate, PermissionRuleValue


def test_outbound_suggestion_uses_to_dict_with_camelCase_rules(real_sdk_required: Any) -> None:
    PermissionUpdate, PermissionRuleValue = real_sdk_required
    suggestion = PermissionUpdate(
        type="addRules",
        behavior="allow",
        rules=[PermissionRuleValue(tool_name="Read", rule_content="/tmp/**")],
        destination="session",
    )
    # ToolPermissionContext-shape with the suggestion attached.
    class _Ctx:
        tool_use_id = "tu_1"
        correlation_id = None
        agent_id = None
        suggestions = [suggestion]

    out = _coerce_context(_Ctx())
    # The wire payload must match the SDK's canonical control-protocol shape
    # (PermissionUpdate.to_dict) — rules use camelCase keys, not Python attrs.
    assert out["suggestions"] == [
        {
            "type": "addRules",
            "destination": "session",
            "rules": [{"toolName": "Read", "ruleContent": "/tmp/**"}],
            "behavior": "allow",
        }
    ]


@pytest.mark.asyncio
async def test_allow_with_updated_permissions_dict_is_hydrated_to_dataclass(
    real_sdk_required: Any,
) -> None:
    """A dict suggestion returned by the client must be rebuilt into a
    PermissionUpdate before reaching the SDK, so the SDK can call .to_dict()."""
    PermissionUpdate, _ = real_sdk_required

    captured: list[tuple[str, dict[str, Any]]] = []

    def emit(ev: str, data: dict[str, Any]) -> None:
        captured.append((ev, data))

    router = PermissionRouter()
    can_use_tool = build_can_use_tool(emit, router)

    import asyncio

    async def resolve_after_emit() -> None:
        await asyncio.sleep(0)
        _, data = captured[0]
        # Simulate the client posting permission_response with an "Always allow" chip.
        router.resolve(data["correlation_id"], {
            "behavior": "allow",
            "updated_permissions": [
                {
                    "type": "addRules",
                    "behavior": "allow",
                    "rules": [{"toolName": "Read", "ruleContent": "/tmp/**"}],
                    "destination": "session",
                }
            ],
        })

    call_task = asyncio.create_task(
        can_use_tool("Read", {"file_path": "/tmp/x"}, {"tool_use_id": "tu_1"})
    )
    resolve_task = asyncio.create_task(resolve_after_emit())
    result, _ = await asyncio.gather(call_task, resolve_task)

    assert result.updated_permissions is not None
    assert len(result.updated_permissions) == 1
    rebuilt = result.updated_permissions[0]
    # It must be the SDK's PermissionUpdate dataclass, not a dict — that's
    # what makes the subsequent .to_dict() call work.
    assert isinstance(rebuilt, PermissionUpdate)
    assert rebuilt.type == "addRules"
    assert rebuilt.behavior == "allow"
    assert rebuilt.destination == "session"
    assert len(rebuilt.rules or []) == 1
    assert rebuilt.rules[0].tool_name == "Read"
    assert rebuilt.rules[0].rule_content == "/tmp/**"
    # Round-trip through to_dict to make sure the SDK's downstream call won't
    # raise — this is the exact failure path the user reported.
    rebuilt.to_dict()


@pytest.mark.asyncio
async def test_allow_with_already_dataclass_permission_passes_through(
    real_sdk_required: Any,
) -> None:
    """If a caller hands us a real PermissionUpdate already, don't double-wrap."""
    PermissionUpdate, _ = real_sdk_required

    captured: list[tuple[str, dict[str, Any]]] = []

    def emit(ev: str, data: dict[str, Any]) -> None:
        captured.append((ev, data))

    router = PermissionRouter()
    can_use_tool = build_can_use_tool(emit, router)

    pre_built = PermissionUpdate(type="setMode", mode="acceptEdits")

    import asyncio

    async def resolve_after_emit() -> None:
        await asyncio.sleep(0)
        _, data = captured[0]
        router.resolve(data["correlation_id"], {
            "behavior": "allow",
            "updated_permissions": [pre_built],
        })

    call_task = asyncio.create_task(
        can_use_tool("Read", {}, {"tool_use_id": "tu_2"})
    )
    resolve_task = asyncio.create_task(resolve_after_emit())
    result, _ = await asyncio.gather(call_task, resolve_task)

    assert result.updated_permissions == [pre_built]
