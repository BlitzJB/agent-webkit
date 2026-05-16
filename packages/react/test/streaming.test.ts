/**
 * L2 streaming tests — exercises the reducer's `message_delta` accumulation
 * and reconciliation against `message_complete` across scenarios that mirror
 * what the server actually emits:
 *
 *   • text_delta-shaped deltas (server uses `{type:"text",text}`)
 *   • raw `{text}` shape (older form; still supported)
 *   • two concurrent assistant messages with distinct message_ids
 *   • final message arriving with extra blocks (tool_use) — content fully replaced
 *   • status transitions: streaming → idle on result
 */
import { describe, it, expect } from "vitest";
import { initialState, reduce, type AgentState, type DisplayMessage } from "../src/reducer.js";
import type { DeliveredEvent } from "@agent-webkit/core";

const ev = <E extends DeliveredEvent>(e: E): E => e;

function feed(state: AgentState, ...events: DeliveredEvent[]): AgentState {
  return events.reduce((s, e) => reduce(s, { type: "server_event", event: e }), state);
}

function assistant(s: AgentState): Extract<DisplayMessage, { kind: "assistant" }> {
  const m = s.messages.find((x) => x.kind === "assistant");
  if (!m || m.kind !== "assistant") throw new Error("no assistant message");
  return m;
}

describe("L2 reducer — message_delta streaming", () => {
  it("accumulates text deltas in `{type:'text',text}` shape (server form)", () => {
    const s = feed(
      initialState,
      ev({ id: 1, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: "Hel" } } }),
      ev({ id: 2, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: "lo" } } }),
      ev({ id: 3, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: " world" } } })
    );
    const m = assistant(s);
    expect(m.content).toEqual([{ type: "text", text: "Hello world" }]);
    expect(m.streaming).toBe(true);
    expect(s.status).toBe("streaming");
  });

  it("keeps two assistant messages distinct when message_ids differ", () => {
    const s = feed(
      initialState,
      ev({ id: 1, event: "message_delta", data: { message_id: "a", delta: { type: "text", text: "first" } } }),
      ev({ id: 2, event: "message_delta", data: { message_id: "b", delta: { type: "text", text: "second" } } })
    );
    const assistants = s.messages.filter((m): m is Extract<DisplayMessage, { kind: "assistant" }> => m.kind === "assistant");
    expect(assistants).toHaveLength(2);
    expect(assistants[0]!.message_id).toBe("a");
    expect(assistants[1]!.message_id).toBe("b");
    expect(assistants[0]!.content).toEqual([{ type: "text", text: "first" }]);
    expect(assistants[1]!.content).toEqual([{ type: "text", text: "second" }]);
  });

  it("message_complete fully replaces streamed content (adds tool_use block)", () => {
    const s = feed(
      initialState,
      ev({ id: 1, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: "Let me check…" } } }),
      ev({
        id: 2,
        event: "message_complete",
        data: {
          message_id: "m",
          message: {
            id: "m",
            role: "assistant",
            content: [
              { type: "text", text: "Let me check the weather." },
              { type: "tool_use", id: "tu_1", name: "get_weather", input: { city: "Boston" } },
            ],
          },
        },
      })
    );
    const m = assistant(s);
    expect(m.streaming).toBe(false);
    expect(m.content).toEqual([
      { type: "text", text: "Let me check the weather." },
      { type: "tool_use", id: "tu_1", name: "get_weather", input: { city: "Boston" } },
    ]);
  });

  it("message_complete arriving before any deltas just appends the assistant message", () => {
    const s = feed(
      initialState,
      ev({
        id: 1,
        event: "message_complete",
        data: {
          message_id: "m",
          message: {
            id: "m",
            role: "assistant",
            content: [{ type: "text", text: "no streaming here" }],
          },
        },
      })
    );
    expect(s.messages).toHaveLength(1);
    const m = assistant(s);
    expect(m.content).toEqual([{ type: "text", text: "no streaming here" }]);
    expect(m.streaming).toBe(false);
  });

  it("`result` after streaming flips status idle and keeps the streamed message", () => {
    const s = feed(
      initialState,
      ev({ id: 1, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: "Hi" } } }),
      ev({
        id: 2,
        event: "message_complete",
        data: { message_id: "m", message: { id: "m", role: "assistant", content: [{ type: "text", text: "Hi" }] } },
      }),
      ev({ id: 3, event: "result", data: { session_id: "s", subtype: "success", total_cost_usd: 0.005 } })
    );
    expect(s.status).toBe("idle");
    expect(s.totalCostUsd).toBeCloseTo(0.005);
    expect(assistant(s).streaming).toBe(false);
  });

  it("supports legacy `{text}` delta shape (no `type` field)", () => {
    // The wire protocol allows `delta: { text }` as a synonym for text deltas.
    const s = feed(
      initialState,
      ev({ id: 1, event: "message_delta", data: { message_id: "m", delta: { text: "Hey " } } }),
      ev({ id: 2, event: "message_delta", data: { message_id: "m", delta: { text: "there" } } })
    );
    expect(assistant(s).content).toEqual([{ type: "text", text: "Hey there" }]);
  });

  it("input_json_delta deltas append as separate blocks (not merged into text)", () => {
    // The reducer doesn't know about GenUI's partial-JSON buffering — that's L1
    // GenUIStream's job. What we *do* care about: input_json_delta must NOT
    // get coerced into the trailing text block or silently dropped.
    const s = feed(
      initialState,
      ev({ id: 1, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: "thinking" } } }),
      ev({
        id: 2,
        event: "message_delta",
        data: {
          message_id: "m",
          delta: { type: "input_json_delta", partial_json: '{"x":', tool_use_id: "tu_1" },
        },
      })
    );
    const m = assistant(s);
    expect(m.content).toHaveLength(2);
    expect(m.content[0]).toEqual({ type: "text", text: "thinking" });
    expect((m.content[1] as { type: string }).type).toBe("input_json_delta");
  });

  it("interleaved deltas for two different message_ids stay isolated", () => {
    const s = feed(
      initialState,
      ev({ id: 1, event: "message_delta", data: { message_id: "a", delta: { type: "text", text: "A1" } } }),
      ev({ id: 2, event: "message_delta", data: { message_id: "b", delta: { type: "text", text: "B1" } } }),
      ev({ id: 3, event: "message_delta", data: { message_id: "a", delta: { type: "text", text: "A2" } } }),
      ev({ id: 4, event: "message_delta", data: { message_id: "b", delta: { type: "text", text: "B2" } } })
    );
    const a = s.messages.find((m): m is Extract<DisplayMessage, { kind: "assistant" }> => m.kind === "assistant" && m.message_id === "a")!;
    const b = s.messages.find((m): m is Extract<DisplayMessage, { kind: "assistant" }> => m.kind === "assistant" && m.message_id === "b")!;
    expect(a.content).toEqual([{ type: "text", text: "A1A2" }]);
    expect(b.content).toEqual([{ type: "text", text: "B1B2" }]);
  });
});
