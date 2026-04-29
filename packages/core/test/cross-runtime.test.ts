import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { spawn, type ChildProcess } from "node:child_process";
import { createAgentClient } from "../src/index.js";
import { GenUIStream } from "../src/genui/index.js";

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

/**
 * GenUI cross-runtime test: the Python GenUIRegistry serves a schema, the server
 * emits a tool_use event for `mcp__genui__render_weather_card`, and the TS L1
 * GenUIStream parses both and produces the right update. This is the only
 * defense against schema-shape or tool-name-format drift between runtimes.
 */
describe.skipIf(skip)("L1 GenUIStream against real Python server", () => {
  let proc: ChildProcess | null = null;
  let port = 0;

  beforeAll(async () => {
    port = 19000 + Math.floor(Math.random() * 1000);
    proc = spawn(
      VENV_PYTHON,
      ["-c", `
import sys, asyncio
sys.path.insert(0, '${REPO_ROOT}packages/agent-webkit-server/src')
sys.path.insert(0, '${REPO_ROOT}')
from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig
from agent_webkit_server.extras.genui import GenUIRegistry, wrap_can_use_tool_for_genui
from tests.fake_claude_sdk import FakeClaudeSDKClient
from pathlib import Path
from pydantic import BaseModel
from typing import Optional
import uvicorn

class WeatherCard(BaseModel):
    """Show weather."""
    location: str
    temperature_f: float
    condition: Optional[str] = None

reg = GenUIRegistry()
reg.register(WeatherCard)

async def factory(_, can_use_tool=None):
    # Apply the GenUI auto-allow wrapper that the default factory would have applied
    # if we hadn't overridden sdk_factory.
    wrapped = wrap_can_use_tool_for_genui(can_use_tool, reg) if can_use_tool else None
    return FakeClaudeSDKClient(Path('${REPO_ROOT}fixtures/genui_render.jsonl'), can_use_tool=wrapped)

app = create_app(auth=AuthConfig(disabled=True), sdk_factory=factory, genui=reg)
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

  it("serves a schema whose qualified names match what the bridge emits", async () => {
    const ui = new GenUIStream({ schemaUrl: `http://127.0.0.1:${port}/genui/schema` });
    const schema = await ui.loadSchema();
    expect(schema).not.toBeNull();
    expect(schema!.server_name).toBe("genui");
    expect(schema!.prefix).toBe("render_");
    expect(schema!.tools.map((t) => t.short_name)).toContain("weather_card");
    const entry = schema!.tools.find((t) => t.short_name === "weather_card")!;
    expect(entry.name).toBe("mcp__genui__render_weather_card");
    expect(entry.raw_tool_name).toBe("render_weather_card");
    // The pydantic schema must round-trip: object with the declared fields.
    expect(entry.schema.type).toBe("object");
    expect(Object.keys(entry.schema.properties ?? {})).toEqual(
      expect.arrayContaining(["location", "temperature_f", "condition"]),
    );
  }, 10_000);

  it("a real tool_use event from Python parses cleanly through the TS GenUIStream", async () => {
    const ui = new GenUIStream({ schemaUrl: `http://127.0.0.1:${port}/genui/schema` });
    await ui.loadSchema();

    const client = createAgentClient({ baseUrl: `http://127.0.0.1:${port}` });
    const session = await client.createSession();
    await session.send("Render Boston weather.");

    let toolUseSeen = false;
    let parsedUpdate: ReturnType<GenUIStream["feed"]> = null;

    for await (const ev of session.events()) {
      if (ev.event === "tool_use") {
        toolUseSeen = true;
        parsedUpdate = ui.feed(ev);
      }
      if (ev.event === "result") break;
    }

    expect(toolUseSeen).toBe(true);
    expect(parsedUpdate).not.toBeNull();
    expect(parsedUpdate!.shortName).toBe("weather_card");
    expect(parsedUpdate!.qualifiedName).toBe("mcp__genui__render_weather_card");
    expect(parsedUpdate!.complete).toBe(true);
    expect(parsedUpdate!.props).toEqual({
      location: "Boston, MA",
      temperature_f: 72,
      condition: "sunny",
    });
    await session.close();
  }, 15_000);
});
