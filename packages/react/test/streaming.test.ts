/**
 * L2 reducer (mux) — streaming text deltas under session_id keying.
 *
 * Same scenarios as the per-session model, just verified through the
 * sessions[sid] slot of MuxState.
 */
import { describe, it, expect } from "vitest";
import { initialMuxState, reduce, type MuxState, type DisplayMessage } from "../src/reducer.js";
import type { DeliveredEvent } from "@agent-webkit/core";

const SID = "sid_X";
const ev = <E extends Omit<DeliveredEvent, "session_id">>(e: E): DeliveredEvent =>
  ({ ...e, session_id: SID } as DeliveredEvent);

function feed(state: MuxState, ...events: DeliveredEvent[]): MuxState {
  return events.reduce((s, e) => reduce(s, { type: "server_event", event: e }), state);
}

function assistant(state: MuxState) {
  const s = state.sessions[SID]!;
  const m = s.messages.find((x) => x.kind === "assistant");
  if (!m || m.kind !== "assistant") throw new Error("no assistant message");
  return m;
}

describe("L2 reducer — message_delta streaming", () => {
  it("accumulates text deltas in `{type:'text',text}` shape (server form)", () => {
    const s = feed(
      initialMuxState,
      ev({ id: 1, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: "Hel" } } } as any),
      ev({ id: 2, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: "lo" } } } as any),
      ev({ id: 3, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: " world" } } } as any)
    );
    const m = assistant(s);
    expect(m.content).toEqual([{ type: "text", text: "Hello world" }]);
    expect(m.streaming).toBe(true);
    expect(s.sessions[SID]!.status).toBe("streaming");
  });

  it("keeps two assistant messages distinct when message_ids differ", () => {
    const s = feed(
      initialMuxState,
      ev({ id: 1, event: "message_delta", data: { message_id: "a", delta: { type: "text", text: "first" } } } as any),
      ev({ id: 2, event: "message_delta", data: { message_id: "b", delta: { type: "text", text: "second" } } } as any)
    );
    const assistants = s.sessions[SID]!.messages.filter(
      (m): m is Extract<DisplayMessage, { kind: "assistant" }> => m.kind === "assistant"
    );
    expect(assistants).toHaveLength(2);
    expect(assistants[0]!.content).toEqual([{ type: "text", text: "first" }]);
    expect(assistants[1]!.content).toEqual([{ type: "text", text: "second" }]);
  });

  it("message_complete fully replaces streamed content (adds tool_use block)", () => {
    const s = feed(
      initialMuxState,
      ev({ id: 1, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: "Let me check…" } } } as any),
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
      } as any)
    );
    const m = assistant(s);
    expect(m.streaming).toBe(false);
    expect(m.content).toEqual([
      { type: "text", text: "Let me check the weather." },
      { type: "tool_use", id: "tu_1", name: "get_weather", input: { city: "Boston" } },
    ]);
  });

  it("message_complete arriving before any deltas just appends", () => {
    const s = feed(
      initialMuxState,
      ev({
        id: 1,
        event: "message_complete",
        data: {
          message_id: "m",
          message: { id: "m", role: "assistant", content: [{ type: "text", text: "no streaming here" }] },
        },
      } as any)
    );
    expect(s.sessions[SID]!.messages).toHaveLength(1);
    const m = assistant(s);
    expect(m.content).toEqual([{ type: "text", text: "no streaming here" }]);
    expect(m.streaming).toBe(false);
  });

  it("`result` flips status idle and keeps the streamed message", () => {
    const s = feed(
      initialMuxState,
      ev({ id: 1, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: "Hi" } } } as any),
      ev({
        id: 2,
        event: "message_complete",
        data: { message_id: "m", message: { id: "m", role: "assistant", content: [{ type: "text", text: "Hi" }] } },
      } as any),
      ev({ id: 3, event: "result", data: { session_id: "s", subtype: "success", total_cost_usd: 0.005 } } as any)
    );
    expect(s.sessions[SID]!.status).toBe("idle");
    expect(s.sessions[SID]!.totalCostUsd).toBeCloseTo(0.005);
    expect(assistant(s).streaming).toBe(false);
  });

  it("supports legacy `{text}` delta shape (no `type` field)", () => {
    const s = feed(
      initialMuxState,
      ev({ id: 1, event: "message_delta", data: { message_id: "m", delta: { text: "Hey " } } } as any),
      ev({ id: 2, event: "message_delta", data: { message_id: "m", delta: { text: "there" } } } as any)
    );
    expect(assistant(s).content).toEqual([{ type: "text", text: "Hey there" }]);
  });

  it("input_json_delta deltas append as separate blocks (not merged into text)", () => {
    const s = feed(
      initialMuxState,
      ev({ id: 1, event: "message_delta", data: { message_id: "m", delta: { type: "text", text: "thinking" } } } as any),
      ev({
        id: 2,
        event: "message_delta",
        data: {
          message_id: "m",
          delta: { type: "input_json_delta", partial_json: '{"x":', tool_use_id: "tu_1" },
        },
      } as any)
    );
    const m = assistant(s);
    expect(m.content).toHaveLength(2);
    expect(m.content[0]).toEqual({ type: "text", text: "thinking" });
    expect((m.content[1] as { type: string }).type).toBe("input_json_delta");
  });

  it("interleaved deltas for two different message_ids stay isolated within one session", () => {
    const s = feed(
      initialMuxState,
      ev({ id: 1, event: "message_delta", data: { message_id: "a", delta: { type: "text", text: "A1" } } } as any),
      ev({ id: 2, event: "message_delta", data: { message_id: "b", delta: { type: "text", text: "B1" } } } as any),
      ev({ id: 3, event: "message_delta", data: { message_id: "a", delta: { type: "text", text: "A2" } } } as any),
      ev({ id: 4, event: "message_delta", data: { message_id: "b", delta: { type: "text", text: "B2" } } } as any)
    );
    const msgs = s.sessions[SID]!.messages;
    const a = msgs.find(
      (m): m is Extract<DisplayMessage, { kind: "assistant" }> => m.kind === "assistant" && m.message_id === "a"
    )!;
    const b = msgs.find(
      (m): m is Extract<DisplayMessage, { kind: "assistant" }> => m.kind === "assistant" && m.message_id === "b"
    )!;
    expect(a.content).toEqual([{ type: "text", text: "A1A2" }]);
    expect(b.content).toEqual([{ type: "text", text: "B1B2" }]);
  });
});
