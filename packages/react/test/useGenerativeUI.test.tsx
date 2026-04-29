/**
 * Hook-level tests for useGenerativeUI.
 *
 * @vitest-environment happy-dom
 */
import { describe, it, expect } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import type { DeliveredEvent } from "@agent-webkit/core";
import type { GenUISchemaPayload, GenUIUpdate } from "@agent-webkit/core/genui";
import { useGenerativeUI } from "../src/useGenerativeUI.js";

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
  } as DeliveredEvent;
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
      delta: {
        type: "input_json_delta",
        tool_use_id: toolUseId,
        partial_json: partial,
        ...(name ? { name } : {}),
      },
    },
  } as unknown as DeliveredEvent;
}

describe("useGenerativeUI", () => {
  it("collects updates from tool_use events and renders via short_name", async () => {
    const { result } = renderHook(() =>
      useGenerativeUI({
        schema: SCHEMA,
        renderers: {
          weather_card: (props) => `<weather:${(props as { location: string }).location}>`,
        },
      }),
    );

    act(() => {
      result.current.onEvent(
        toolUseEvent("mcp__genui__render_weather_card", { location: "Boston" }),
      );
    });

    expect(result.current.updates).toHaveLength(1);
    const u = result.current.updates[0]!;
    expect(u.shortName).toBe("weather_card");
    expect(u.complete).toBe(true);
    expect(result.current.render(u)).toBe("<weather:Boston>");
  });

  it("ignores events for unregistered tools", () => {
    const { result } = renderHook(() =>
      useGenerativeUI({ schema: SCHEMA, renderers: {} }),
    );
    act(() => {
      result.current.onEvent(toolUseEvent("ReadFile", { path: "x" }));
    });
    expect(result.current.updates).toHaveLength(0);
  });

  it("returns null from render() when no renderer is registered for the short_name", () => {
    const { result } = renderHook(() =>
      useGenerativeUI({ schema: SCHEMA, renderers: {} }),
    );
    act(() => {
      result.current.onEvent(toolUseEvent("mcp__genui__render_weather_card", {}));
    });
    const u = result.current.updates[0]!;
    expect(result.current.render(u)).toBeNull();
  });

  it("merges streamed deltas into a single update keyed by tool_use_id", () => {
    const { result } = renderHook(() =>
      useGenerativeUI({
        schema: SCHEMA,
        renderers: {
          weather_card: (props) => JSON.stringify(props),
        },
      }),
    );

    act(() => {
      result.current.onEvent(
        deltaEvent(`{"location"`, "tu-9", "m-1", 1, "mcp__genui__render_weather_card"),
      );
    });
    expect(result.current.updates).toHaveLength(1);
    expect(result.current.updates[0]!.partial).toBe(true);

    act(() => {
      result.current.onEvent(deltaEvent(`:"Boston"`, "tu-9"));
    });
    expect(result.current.updates).toHaveLength(1);
    expect(result.current.updates[0]!.props).toEqual({ location: "Boston" });

    act(() => {
      result.current.onEvent(
        toolUseEvent(
          "mcp__genui__render_weather_card",
          { location: "Boston", temperature_f: 72 },
          "tu-9",
          "m-1",
          5,
        ),
      );
    });
    expect(result.current.updates).toHaveLength(1);
    expect(result.current.updates[0]!.complete).toBe(true);
    expect(result.current.updates[0]!.props).toEqual({ location: "Boston", temperature_f: 72 });
  });

  it("auto-loads schema from schemaUrl on mount", async () => {
    const fetchImpl = (async () => ({
      ok: true,
      json: async () => SCHEMA,
    })) as unknown as typeof fetch;

    const { result } = renderHook(() =>
      useGenerativeUI({
        schemaUrl: "http://x/genui/schema",
        fetchImpl,
        renderers: {
          weather_card: (props) => `loaded:${(props as { x: number }).x}`,
        },
      }),
    );

    await waitFor(() => {
      expect(result.current.schema?.tools.length).toBe(1);
    });

    act(() => {
      result.current.onEvent(toolUseEvent("mcp__genui__render_weather_card", { x: 1 }));
    });
    expect(result.current.updates).toHaveLength(1);
  });

  it("propagates byte-by-byte fragmented stream without tearing or losing data", () => {
    // Capture every observable state of `updates` after each byte.
    const observed: GenUIUpdate[][] = [];
    const { result } = renderHook(() =>
      useGenerativeUI({
        schema: SCHEMA,
        renderers: {
          weather_card: (props) => JSON.stringify(props),
        },
      }),
    );

    const fullJson = '{"location":"Boston, MA","temperature_f":72,"condition":"sunny"}';
    // Drip one character at a time, taking a snapshot after each drip in its
    // own act() so React commits between bytes — this is the worst-case
    // delivery pattern the partial-JSON parser must survive.
    for (let i = 0; i < fullJson.length; i++) {
      act(() => {
        result.current.onEvent(
          deltaEvent(fullJson[i]!, "tu-frag", "m-1", i + 1, "mcp__genui__render_weather_card"),
        );
      });
      observed.push(result.current.updates);
    }

    // Always exactly one update entry — the partial parser must merge every
    // delta into the same tool_use_id, never duplicate it.
    for (const snap of observed) {
      expect(snap).toHaveLength(1);
      expect(snap[0]!.toolUseId).toBe("tu-frag");
      expect(snap[0]!.shortName).toBe("weather_card");
    }

    // Final byte yields fully parsed props.
    const last = observed[observed.length - 1]![0]!;
    expect(last.props).toEqual({
      location: "Boston, MA",
      temperature_f: 72,
      condition: "sunny",
    });

    // Monotonic recovery: once `location` appears as a string, it must only
    // grow (or hold steady) until reaching its final value. No rewind.
    let lastLocation = "";
    for (const snap of observed) {
      const loc = (snap[0]!.props as { location?: string }).location;
      if (typeof loc === "string") {
        expect(loc.startsWith(lastLocation) || lastLocation.startsWith(loc)).toBe(true);
        if (loc.length >= lastLocation.length) lastLocation = loc;
      }
    }
    expect(lastLocation).toBe("Boston, MA");

    // Finalizing tool_use does not duplicate the entry.
    act(() => {
      result.current.onEvent(
        toolUseEvent(
          "mcp__genui__render_weather_card",
          { location: "Boston, MA", temperature_f: 72, condition: "sunny" },
          "tu-frag",
          "m-1",
          999,
        ),
      );
    });
    expect(result.current.updates).toHaveLength(1);
    expect(result.current.updates[0]!.complete).toBe(true);
  });

  it("reset() clears updates", () => {
    const { result } = renderHook(() =>
      useGenerativeUI({ schema: SCHEMA, renderers: {} }),
    );
    act(() => {
      result.current.onEvent(toolUseEvent("mcp__genui__render_weather_card", {}));
    });
    expect(result.current.updates).toHaveLength(1);
    act(() => result.current.reset());
    expect(result.current.updates).toHaveLength(0);
  });
});
