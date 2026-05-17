/**
 * L2 reducer (mux) — user_message handling.
 *
 * Pure replay: server `user_message` arrives, no prior optimistic insert →
 * appended as a user-kind DisplayMessage in the target session's slot.
 *
 * Live send: local_user_message inserts immediately, server echo dedupes.
 */
import { describe, it, expect } from "vitest";
import { initialMuxState, reduce } from "../src/reducer.js";
import type { DeliveredEvent } from "@agent-webkit/core";

const SID = "sid_X";
const ev = <E extends Omit<DeliveredEvent, "session_id">>(e: E): DeliveredEvent =>
  ({ ...e, session_id: SID } as DeliveredEvent);

describe("user_message replay (mux)", () => {
  it("appends the user prompt on a clean replay (no local insert)", () => {
    const s = reduce(initialMuxState, {
      type: "server_event",
      event: ev({ id: 1, event: "user_message", data: { content: "hello" } } as any),
    });
    expect(s.sessions[SID]!.messages).toHaveLength(1);
    const m = s.sessions[SID]!.messages[0]!;
    expect(m.kind).toBe("user");
    if (m.kind === "user") expect(m.content).toBe("hello");
  });

  it("dedupes the server echo against an optimistic local_user_message", () => {
    let s = reduce(initialMuxState, {
      type: "local_user_message",
      sessionId: SID,
      content: "hello",
      localId: "local-1",
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 7, event: "user_message", data: { content: "hello" } } as any),
    });
    expect(s.sessions[SID]!.messages).toHaveLength(1);
    expect(s.sessions[SID]!.messages[0]!.id).toBe("local-1");
  });

  it("appends both when contents differ", () => {
    let s = reduce(initialMuxState, {
      type: "local_user_message",
      sessionId: SID,
      content: "hello",
      localId: "local-1",
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 7, event: "user_message", data: { content: "different" } } as any),
    });
    expect(s.sessions[SID]!.messages).toHaveLength(2);
  });

  it("interleaves user → assistant → user in order within the session", () => {
    let s = initialMuxState;
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 1, event: "user_message", data: { content: "q1" } } as any),
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
      } as any),
    });
    s = reduce(s, {
      type: "server_event",
      event: ev({ id: 3, event: "user_message", data: { content: "q2" } } as any),
    });
    expect(s.sessions[SID]!.messages.map((m) => m.kind)).toEqual(["user", "assistant", "user"]);
  });
});
