import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { createAgentClient } from "../src/index.js";

/**
 * Cross-runtime test: boot the real Python FastAPI server (with the fake SDK as factory),
 * then drive it from the L1 client. This is the golden integration check that the wire
 * protocol contract holds across implementations.
 *
 * Skipped automatically if Python venv isn't set up or if `AGENT_WEBKIT_SKIP_CROSS=1`.
 */

const REPO_ROOT = new URL("../../../", import.meta.url).pathname;
const VENV_PYTHON = `${REPO_ROOT}.venv/bin/python`;

async function waitForServer(url: string, timeoutMs = 5000): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(url, { method: "POST", body: "{}", headers: { "content-type": "application/json" } });
      if (res.ok || res.status === 401 || res.status === 422) return true;
    } catch {
      // server not ready
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  return false;
}

const fs = await import("node:fs");
const venvAvailable = fs.existsSync(VENV_PYTHON);
const skip = !venvAvailable || process.env["AGENT_WEBKIT_SKIP_CROSS"] === "1";

describe.skipIf(skip)("L1 against real Python server", () => {
  let proc: ChildProcess | null = null;
  let port = 0;

  beforeAll(async () => {
    port = 18000 + Math.floor(Math.random() * 1000);
    proc = spawn(
      VENV_PYTHON,
      ["-c", `
import sys, asyncio
sys.path.insert(0, '${REPO_ROOT}packages/agent-webkit-server/src')
sys.path.insert(0, '${REPO_ROOT}')
from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig
from tests.fake_claude_sdk import FakeClaudeSDKClient
from pathlib import Path
import uvicorn

async def factory(_, can_use_tool=None):
    return FakeClaudeSDKClient(Path('${REPO_ROOT}fixtures/plain_qa.jsonl'), can_use_tool=can_use_tool)

app = create_app(auth=AuthConfig(disabled=True), sdk_factory=factory)
uvicorn.run(app, host='127.0.0.1', port=${port}, log_level='warning')
      `],
      { stdio: ["ignore", "pipe", "pipe"], cwd: REPO_ROOT }
    );
    const ok = await waitForServer(`http://127.0.0.1:${port}/sessions`, 8000);
    if (!ok) throw new Error("Python server failed to start");
  }, 15_000);

  afterAll(() => {
    if (proc && !proc.killed) proc.kill("SIGTERM");
  });

  it("creates a session, sends a message, receives session_ready + message_complete + result", async () => {
    const client = createAgentClient({ baseUrl: `http://127.0.0.1:${port}` });
    const session = await client.createSession();
    expect(session.protocolVersion).toBe("1.0");

    await session.send("hi");

    const events: { event: string; id: number }[] = [];
    for await (const ev of session.events()) {
      events.push({ event: ev.event, id: ev.id });
      if (ev.event === "result") break;
    }
    const names = events.map((e) => e.event);
    expect(names[0]).toBe("session_ready");
    expect(names).toContain("message_complete");
    expect(names[names.length - 1]).toBe("result");
    await session.close();
  }, 15_000);
});
