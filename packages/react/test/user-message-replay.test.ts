/**
 * L2 reducer — `user_message` wire event handling.
 *
 * For *replay* (no prior optimistic insert): append the prompt as a
 * user-kind DisplayMessage so attaching mid-conversation reconstructs the
 * full transcript.
 *
 * For *live send* (we already did local_user_message): dedupe by content
 * match against the most recent user message so the bubble doesn't
 * duplicate when the server's echo arrives milliseconds later.
 */
import { describe, it, expect } from "vitest";
import { initialState, reduce } from "../src/reducer.js";
import type { DeliveredEvent } from "@agent-webkit/core";

const ev = <E extends DeliveredEvent>(e: E): E => e;

describe("user_message replay", () => {
  it("appends the user prompt on a clean replay (no local insert)", () => {
    const s = reduce(initialState, {
      type: "server_event",
      event: ev({ id: 1, event: "user_message", data: { content: "hello" } }),
    });
    expect(s.messages).toHaveLength(1);
    const m = s.messages[0]!;
    expect(m.kind).toBe("user");
    if (m.kind === "user") expect(m.content).toBe("hello");
  });

  it("dedupes the server echo against an optimistic local_user_message", () => {
    let s = reduce(initialState, {
      type: "local_user_message",
      content: "hello",
      localId: "local-1",
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 7, event: "user_message", data: { content: "hello" } }),
    });
    // Still exactly one user bubble — the optimistic one.
    expect(s.messages).toHaveLength(1);
    expect(s.messages[0]!.id).toBe("local-1");
  });

  it("appends both when contents differ", () => {
    let s = reduce(initialState, {
      type: "local_user_message",
      content: "hello",
      localId: "local-1",
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 7, event: "user_message", data: { content: "different" } }),
    });
    expect(s.messages).toHaveLength(2);
  });

  it("interleaves user prompt → assistant reply → user prompt in order", () => {
    let s = initialState;
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 1, event: "user_message", data: { content: "q1" } }),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({
        id: 2,
        event: "message_complete",
        data: {
          message_id: "m1",
          message: { id: "m1", role: "assistant", content: [{ type: "text", text: "a1" }] },
        },
      }),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 3, event: "user_message", data: { content: "q2" } }),
    });
    expect(s.messages.map((m) => m.kind)).toEqual(["user", "assistant", "user"]);
  });
});
