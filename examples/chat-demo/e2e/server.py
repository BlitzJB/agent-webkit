"""Launches the reference server with the in-process fake SDK for E2E tests."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import uvicorn

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "server-reference"))
sys.path.insert(0, str(ROOT))

from server.auth import AuthConfig  # noqa: E402
from server.main import create_app  # noqa: E402
from server.session import SessionConfig  # noqa: E402
from tests.fake_claude_sdk import FakeClaudeSDKClient  # noqa: E402

FIXTURE = ROOT / "fixtures" / "plain_qa.jsonl"


async def factory(_config: SessionConfig, can_use_tool: Any = None) -> Any:
    return FakeClaudeSDKClient(FIXTURE, can_use_tool=can_use_tool)


def main() -> None:
    app = create_app(auth=AuthConfig(disabled=True), sdk_factory=factory)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
