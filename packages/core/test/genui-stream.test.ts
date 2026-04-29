import { describe, it, expect } from "vitest";
import { GenUIStream } from "../src/genui/stream.js";
import type { DeliveredEvent } from "../src/types.js";
import type { GenUISchemaPayload } from "../src/genui/types.js";

const SCHEMA: GenUISchemaPayload = {
  version: "1.0",
  server_name: "genui",
  prefix: "render_",
  tools: [
    {
      name: "mcp__genui__render_weather_card",
      short_name: "weather_card",
      raw_tool_name: "render_weather_card",
      description: "show weather",
      schema: { type: "object" },
    },
    {
      name: "mcp__genui__render_pricing_table",
      short_name: "pricing_table",
      raw_tool_name: "render_pricing_table",
      description: "show pricing",
      schema: { type: "object" },
    },
  ],
};

function toolUseEvent(
  toolName: string,
  input: Record<string, unknown> = {},
  toolUseId = "tu-1",
  messageId = "m-1",
  id = 1,
): DeliveredEvent {
  return {
    id,
    event: "tool_use",
    data: { message_id: messageId, tool_use_id: toolUseId, tool_name: toolName, input },
  };
}

function deltaEvent(
  partial: string,
  toolUseId = "tu-1",
  messageId = "m-1",
  id = 1,
  name?: string,
): DeliveredEvent {
  return {
    id,
    event: "message_delta",
    data: {
      message_id: messageId,
      delta: { type: "input_json_delta", tool_use_id: toolUseId, partial_json: partial, ...(name ? { name } : {}) },
    },
  } as unknown as DeliveredEvent;
}

describe("GenUIStream — schema-driven matching", () => {
  it("dispatches a complete tool_use to the matching short_name", () => {
    const ui = new GenUIStream({ schema: SCHEMA });
    const ev = toolUseEvent("mcp__genui__render_weather_card", {
      location: "Boston",
      temperature_f: 72,
    });
    const update = ui.feed(ev);
    expect(update).toMatchObject({
      shortName: "weather_card",
      qualifiedName: "mcp__genui__render_weather_card",
      props: { location: "Boston", temperature_f: 72 },
      partial: false,
      complete: true,
      toolUseId: "tu-1",
    });
  });

  it("ignores tool_use events for unregistered tools", () => {
    const ui = new GenUIStream({ schema: SCHEMA });
    const update = ui.feed(toolUseEvent("ReadFile", {}));
    expect(update).toBeNull();
  });

  it("ignores other event kinds", () => {
    const ui = new GenUIStream({ schema: SCHEMA });
    const update = ui.feed({
      id: 1,
      event: "session_ready",
      data: { session_id: "s", protocol_version: "1.0" },
    });
    expect(update).toBeNull();
  });
});

describe("GenUIStream — prefix fallback (no schema)", () => {
  it("falls back to mcp__<server>__<prefix>... matching when no schema is loaded", () => {
    const ui = new GenUIStream({ serverName: "genui", prefix: "render_" });
    const update = ui.feed(toolUseEvent("mcp__genui__render_weather_card", { x: 1 }));
    expect(update).toMatchObject({
      shortName: "weather_card",
      qualifiedName: "mcp__genui__render_weather_card",
      props: { x: 1 },
    });
  });

  it("does not match when prefix differs", () => {
    const ui = new GenUIStream({ serverName: "genui", prefix: "render_" });
    expect(ui.feed(toolUseEvent("mcp__other__render_weather_card", {}))).toBeNull();
    expect(ui.feed(toolUseEvent("mcp__genui__show_weather_card", {}))).toBeNull();
  });
});

describe("GenUIStream — streaming via input_json_delta", () => {
  it("emits partial updates as the buffer parses cleanly", () => {
    const ui = new GenUIStream({ schema: SCHEMA });
    // First delta: needs to know which tool. We provide name on the first delta.
    let r = ui.feed(deltaEvent(`{"location"`, "tu-1", "m-1", 1, "mcp__genui__render_weather_card"));
    expect(r?.props).toEqual({});
    expect(r?.partial).toBe(true);

    r = ui.feed(deltaEvent(`:"Boston"`));
    expect(r?.props).toEqual({ location: "Boston" });

    r = ui.feed(deltaEvent(`,"temperature_f":72`));
    // Number not yet delimited.
    expect(r?.props).toEqual({ location: "Boston" });

    r = ui.feed(deltaEvent(`,"condition":"sunny"}`));
    expect(r?.props).toEqual({
      location: "Boston",
      temperature_f: 72,
      condition: "sunny",
    });
    // Final wire close (`}`) makes complete:true at the partial-parse level, but
    // GenUIStream still calls this `complete:false` until the canonical `tool_use`
    // event arrives. The L2 layer can choose either signal.
    expect(r?.complete).toBe(false);
  });

  it("a final tool_use event supersedes any pending partial state", () => {
    const ui = new GenUIStream({ schema: SCHEMA });
    ui.feed(deltaEvent(`{"location":"Boston"`, "tu-7", "m-1", 1, "mcp__genui__render_weather_card"));
    const final = ui.feed(toolUseEvent("mcp__genui__render_weather_card", {
      location: "Boston",
      temperature_f: 72,
      condition: "sunny",
    }, "tu-7", "m-1", 2));
    expect(final?.complete).toBe(true);
    expect(final?.props).toEqual({ location: "Boston", temperature_f: 72, condition: "sunny" });
  });

  it("ignores deltas for tool_use_ids that have already settled", () => {
    const ui = new GenUIStream({ schema: SCHEMA });
    ui.feed(toolUseEvent("mcp__genui__render_weather_card", { x: 1 }, "tu-9", "m-1", 1));
    const r = ui.feed(deltaEvent(`{"x":2`, "tu-9"));
    expect(r).toBeNull();
  });

  it("multiple concurrent tool_uses are tracked independently", () => {
    const ui = new GenUIStream({ schema: SCHEMA });
    ui.feed(deltaEvent(`{"location":`, "tu-A", "m-1", 1, "mcp__genui__render_weather_card"));
    ui.feed(deltaEvent(`{"plans":[`, "tu-B", "m-1", 2, "mcp__genui__render_pricing_table"));

    const a = ui.feed(deltaEvent(`"NYC"`, "tu-A"));
    expect(a?.shortName).toBe("weather_card");
    expect(a?.props).toEqual({ location: "NYC" });

    const b = ui.feed(deltaEvent(`{"name":"basic"}]`, "tu-B"));
    expect(b?.shortName).toBe("pricing_table");
    expect(b?.props).toEqual({ plans: [{ name: "basic" }] });
  });

  it("reset() clears pending deltas and settled ids", () => {
    const ui = new GenUIStream({ schema: SCHEMA });
    ui.feed(toolUseEvent("mcp__genui__render_weather_card", { x: 1 }, "tu-1", "m-1", 1));
    ui.reset();
    // After reset, a delta for the same tool_use_id should be tracked again.
    const r = ui.feed(deltaEvent(`{"x":`, "tu-1", "m-1", 2, "mcp__genui__render_weather_card"));
    expect(r).not.toBeNull();
  });
});

describe("GenUIStream — schema fetching", () => {
  it("loadSchema fetches and caches", async () => {
    const fetchImpl: typeof fetch = (async () => ({
      ok: true,
      json: async () => SCHEMA,
    })) as unknown as typeof fetch;
    const ui = new GenUIStream({ schemaUrl: "http://x/genui/schema", fetchImpl });
    const s = await ui.loadSchema();
    expect(s?.tools.length).toBe(2);
    // After load, dispatch should work.
    const r = ui.feed(toolUseEvent("mcp__genui__render_weather_card", { x: 1 }));
    expect(r?.shortName).toBe("weather_card");
  });

  it("loadSchema rejects on non-2xx", async () => {
    const fetchImpl: typeof fetch = (async () => ({ ok: false, status: 503 })) as unknown as typeof fetch;
    const ui = new GenUIStream({ schemaUrl: "http://x/genui/schema", fetchImpl });
    await expect(ui.loadSchema()).rejects.toThrow(/503/);
  });
});
