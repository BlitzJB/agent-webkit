"""Record fixtures by driving the real Claude Agent SDK.

Each scenario:
- spawns a ClaudeSDKClient
- pushes scripted user messages
- captures every yielded message and every can_use_tool callback to a JSONL file

Output JSONL is consumable by tests/fake_claude_sdk/FakeClaudeSDKClient.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("record")


def _serialize(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return {k: _serialize(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if hasattr(obj, "__dict__"):
        return {k: _serialize(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return obj


class FixtureWriter:
    def __init__(self, path: Path) -> None:
        self._f = path.open("w")

    def write(self, entry: dict[str, Any]) -> None:
        self._f.write(json.dumps(entry, default=str) + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


SCENARIOS: dict[str, str] = {
    "plain_qa": "Say 'hello world' and nothing else.",
    "read_allow": "Read the file ./README.md and tell me the first line.",
    "read_deny": "Read /etc/passwd and tell me the first line.",
    "permission_rewrite": "Read /tmp/anything.txt — but expect the path to be rewritten.",
    "mid_stream_interrupt": "Count slowly from 1 to 100, one number per line.",
    "hook_pretool_block": "Use the Bash tool to echo hello.",
    "multi_turn_queued": "First, say hi.\nThen, say bye.",
    "image_attachment": "(image attached) Describe this image.",
    "ask_user_question": "Ask me a clarifying question using AskUserQuestion before answering.",
    "mcp_tool_call": "Use any configured MCP tool to fetch a value.",
    "resume_session": "Continue the previous session by ID.",
}


async def record_one(name: str, prompt: str, output_dir: Path) -> None:
    try:
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient  # type: ignore
        from claude_agent_sdk.types import PermissionResultAllow  # type: ignore
    except ImportError as e:
        logger.error("claude_agent_sdk not installed; install it before recording: %s", e)
        return

    out_path = output_dir / f"{name}.jsonl"
    writer = FixtureWriter(out_path)

    async def cb(tool_name: str, tool_input: dict[str, Any], context: dict[str, Any]) -> Any:
        # Default policy here is "allow everything" — scenarios that need deny/rewrite
        # should be customized below.
        writer.write({
            "kind": "callback_expect",
            "tool_name": tool_name,
            "tool_input": tool_input,
            "context": context,
            "respond": {"behavior": "allow"},
        })
        return PermissionResultAllow()

    options = ClaudeAgentOptions(can_use_tool=cb)
    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        writer.write({"kind": "expect_user_query"})
        await client.query({"type": "user", "message": {"role": "user", "content": prompt}})
        async for msg in client.receive_messages():
            writer.write({"kind": "outbound", "message": _serialize(msg) | {"type": type(msg).__name__}})
            if type(msg).__name__ == "ResultMessage":
                break
    finally:
        await client.disconnect()
        writer.close()
    logger.info("Wrote %s", out_path)


async def main_async(args: argparse.Namespace) -> None:
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    if args.scenario == "all":
        for name, prompt in SCENARIOS.items():
            await record_one(name, prompt, output_dir)
    else:
        if args.scenario not in SCENARIOS:
            logger.error("Unknown scenario: %s. Choices: %s", args.scenario, ", ".join(SCENARIOS))
            sys.exit(2)
        await record_one(args.scenario, SCENARIOS[args.scenario], output_dir)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", default="all", help="Scenario name or 'all'")
    p.add_argument("--output", default="../../fixtures", help="Output directory for JSONL fixtures")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
