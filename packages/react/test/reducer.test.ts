import { describe, it, expect } from "vitest";
import { initialState, reduce } from "../src/reducer.js";
import type { DeliveredEvent } from "@agent-webkit/core";

const ev = <E extends DeliveredEvent>(e: E): E => e;

describe("reducer", () => {
  it("appends streaming text deltas to the same assistant message", () => {
    let s = initialState;
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 1, event: "message_delta", data: { message_id: "m1", delta: { text: "Hel" } } }),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 2, event: "message_delta", data: { message_id: "m1", delta: { text: "lo" } } }),
    });
    expect(s.messages).toHaveLength(1);
    const m = s.messages[0]!;
    expect(m.kind).toBe("assistant");
    if (m.kind === "assistant") {
      expect(m.content).toEqual([{ type: "text", text: "Hello" }]);
      expect(m.streaming).toBe(true);
    }
    expect(s.status).toBe("streaming");
  });

  it("reconciles to message_complete content (delta replaced)", () => {
    let s = initialState;
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 1, event: "message_delta", data: { message_id: "m1", delta: { text: "partial" } } }),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({
        id: 2,
        event: "message_complete",
        data: {
          message_id: "m1",
          message: {
            id: "m1",
            role: "assistant",
            content: [{ type: "text", text: "final" }],
          },
        },
      }),
    });
    const m = s.messages[0]!;
    if (m.kind === "assistant") {
      expect(m.content).toEqual([{ type: "text", text: "final" }]);
      expect(m.streaming).toBe(false);
    }
  });

  it("sets pendingPermission and awaiting_permission status", () => {
    const s = reduce(initialState, {
      type: "server_event",
      event: ev({
        id: 1,
        event: "permission_request",
        data: { correlation_id: "tu_1", tool_name: "Read", input: { path: "/x" } },
      }),
    });
    expect(s.status).toBe("awaiting_permission");
    expect(s.pendingPermission?.correlation_id).toBe("tu_1");
  });

  it("clears pendingPermission on resolve", () => {
    let s = reduce(initialState, {
      type: "server_event",
      event: ev({
        id: 1,
        event: "permission_request",
        data: { correlation_id: "tu_1", tool_name: "Read", input: {} },
      }),
    });
    s = reduce(s, { type: "permission_resolved", correlationId: "tu_1" });
    expect(s.pendingPermission).toBeNull();
    expect(s.status).toBe("streaming");
  });

  it("accumulates total_cost_usd from result events", () => {
    let s = reduce(initialState, {
      type: "server_event",
      event: ev({
        id: 1,
        event: "result",
        data: { session_id: "s1", subtype: "success", total_cost_usd: 0.01 },
      }),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({
        id: 2,
        event: "result",
        data: { session_id: "s1", subtype: "success", total_cost_usd: 0.02 },
      }),
    });
    expect(s.totalCostUsd).toBeCloseTo(0.03);
    expect(s.status).toBe("idle");
  });
});
