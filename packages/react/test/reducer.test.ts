/**
 * L2 reducer — keyed-by-session-id mux state.
 *
 * Every action targets a specific session_id. The same event types behave
 * exactly like the pre-multiplex reducer did, but inside the appropriate
 * per-session slot.
 */
import { describe, it, expect } from "vitest";
import { initialMuxState, reduce, type MuxState } from "../src/reducer.js";
import type { DeliveredEvent } from "@agent-webkit/core";

const ev = <E extends Omit<DeliveredEvent, "session_id">>(e: E, sid: string): DeliveredEvent =>
  ({ ...e, session_id: sid } as DeliveredEvent);

function assistant(state: MuxState, sid: string) {
  const s = state.sessions[sid];
  if (!s) throw new Error(`no session ${sid}`);
  const m = s.messages.find((x) => x.kind === "assistant");
  if (!m || m.kind !== "assistant") throw new Error("no assistant message");
  return m;
}

describe("reducer (mux)", () => {
  it("appends streaming text deltas into the right session's slot", () => {
    let s = initialMuxState;
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 1, event: "message_delta", data: { message_id: "m1", delta: { text: "Hel" } } } as any, "sid_A"),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 2, event: "message_delta", data: { message_id: "m1", delta: { text: "lo" } } } as any, "sid_A"),
    });
    const a = assistant(s, "sid_A");
    expect(a.content).toEqual([{ type: "text", text: "Hello" }]);
    expect(a.streaming).toBe(true);
    expect(s.sessions["sid_A"]!.status).toBe("streaming");
    // Other sessions untouched.
    expect(s.sessions["sid_B"]).toBeUndefined();
  });

  it("interleaved events for different sessions don't bleed into each other", () => {
    let s = initialMuxState;
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 1, event: "message_delta", data: { message_id: "mA", delta: { text: "A1" } } } as any, "sid_A"),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 2, event: "message_delta", data: { message_id: "mB", delta: { text: "B1" } } } as any, "sid_B"),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 3, event: "message_delta", data: { message_id: "mA", delta: { text: "A2" } } } as any, "sid_A"),
    });
    expect(assistant(s, "sid_A").content).toEqual([{ type: "text", text: "A1A2" }]);
    expect(assistant(s, "sid_B").content).toEqual([{ type: "text", text: "B1" }]);
  });

  it("message_complete in session A doesn't reset session B", () => {
    let s = initialMuxState;
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 1, event: "message_delta", data: { message_id: "mB", delta: { text: "still streaming" } } } as any, "sid_B"),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({
        id: 2,
        event: "message_complete",
        data: {
          message_id: "mA",
          message: { id: "mA", role: "assistant", content: [{ type: "text", text: "done" }] },
        },
      } as any, "sid_A"),
    });
    expect(s.sessions["sid_A"]!.messages[0]?.kind).toBe("assistant");
    expect(assistant(s, "sid_B").streaming).toBe(true);
  });

  it("local_user_message + matching server echo dedupes in the right session", () => {
    let s = initialMuxState;
    s = reduce(s, { type: "local_user_message", sessionId: "sid_A", content: "hi", localId: "loc-1" });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 7, event: "user_message", data: { content: "hi" } } as any, "sid_A"),
    });
    expect(s.sessions["sid_A"]!.messages).toHaveLength(1);
    expect(s.sessions["sid_A"]!.messages[0]!.id).toBe("loc-1");
  });

  it("permission_request fills only the targeted session's pendingPermission", () => {
    let s = initialMuxState;
    s = reduce(s, {
      type: "server_event",
      event: ev({
        id: 1,
        event: "permission_request",
        data: { correlation_id: "tu_1", tool_name: "Read", input: {} },
      } as any, "sid_A"),
    });
    expect(s.sessions["sid_A"]!.pendingPermission?.correlation_id).toBe("tu_1");
    expect(s.sessions["sid_A"]!.status).toBe("awaiting_permission");
    expect(s.sessions["sid_B"]).toBeUndefined();
  });

  it("history_loaded seeds past messages without disturbing other sessions", () => {
    let s = initialMuxState;
    s = reduce(s, { type: "ensure_session", sessionId: "sid_B" });
    s = reduce(s, {
      type: "history_loaded",
      sessionId: "sid_A",
      events: [
        { event: "user_message", payload: { content: "hi" } },
        {
          event: "message_complete",
          payload: {
            message_id: "m1",
            message: { id: "m1", role: "assistant", content: [{ type: "text", text: "hey" }] },
          },
        },
      ],
    });
    expect(s.sessions["sid_A"]!.messages.map((m) => m.kind)).toEqual(["user", "assistant"]);
    expect(s.sessions["sid_A"]!.status).toBe("idle");
    expect(s.sessions["sid_B"]!.messages).toEqual([]);
  });

  it("ensure_session is idempotent and creates only when missing", () => {
    let s = initialMuxState;
    s = reduce(s, { type: "ensure_session", sessionId: "sid_X" });
    const before = s.sessions["sid_X"];
    s = reduce(s, { type: "ensure_session", sessionId: "sid_X" });
    expect(s.sessions["sid_X"]).toBe(before);
  });

  it("remove_session drops the slot", () => {
    let s = initialMuxState;
    s = reduce(s, { type: "ensure_session", sessionId: "sid_Y" });
    s = reduce(s, { type: "remove_session", sessionId: "sid_Y" });
    expect(s.sessions["sid_Y"]).toBeUndefined();
  });

  it("result event accumulates totalCostUsd only in its own session", () => {
    let s = initialMuxState;
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 1, event: "result", data: { session_id: "x", subtype: "success", total_cost_usd: 0.01 } } as any, "sid_A"),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 2, event: "result", data: { session_id: "y", subtype: "success", total_cost_usd: 0.02 } } as any, "sid_B"),
    });
    expect(s.sessions["sid_A"]!.totalCostUsd).toBeCloseTo(0.01);
    expect(s.sessions["sid_B"]!.totalCostUsd).toBeCloseTo(0.02);
  });
});
