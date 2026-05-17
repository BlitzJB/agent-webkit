/**
 * L2 reducer — permission_mode_changed wire event.
 *
 * On-demand mode toggles flow through the global multiplexed log so every
 * subscriber sees the change. Verifies the reducer routes only into the
 * target session's slot, ignores garbage payloads, and is idempotent.
 */
import { describe, it, expect } from "vitest";
import { initialMuxState, reduce, initialSessionState } from "../src/reducer.js";
import type { DeliveredEvent } from "@agent-webkit/core";

const ev = <E extends Omit<DeliveredEvent, "session_id">>(sid: string, e: E): DeliveredEvent =>
  ({ ...e, session_id: sid } as DeliveredEvent);

describe("permission_mode_changed", () => {
  it("starts with permissionMode null in the initial session state", () => {
    expect(initialSessionState.permissionMode).toBe(null);
  });

  it("sets permissionMode on the addressed session only", () => {
    let s = reduce(initialMuxState, { type: "ensure_session", sessionId: "A" });
    s = reduce(s, { type: "ensure_session", sessionId: "B" });
    s = reduce(s, {
      type: "server_event",
      event: ev("A", { id: 1, event: "permission_mode_changed", data: { mode: "plan" } } as any),
    });
    expect(s.sessions.A!.permissionMode).toBe("plan");
    expect(s.sessions.B!.permissionMode).toBe(null);
  });

  it("creates a session slot on first sight if needed", () => {
    const s = reduce(initialMuxState, {
      type: "server_event",
      event: ev("fresh", { id: 1, event: "permission_mode_changed", data: { mode: "acceptEdits" } } as any),
    });
    expect(s.sessions.fresh!.permissionMode).toBe("acceptEdits");
  });

  it("ignores events whose data.mode is not a string", () => {
    let s = reduce(initialMuxState, { type: "ensure_session", sessionId: "X" });
    s = reduce(s, {
      type: "server_event",
      event: ev("X", { id: 1, event: "permission_mode_changed", data: { mode: null } } as any),
    });
    expect(s.sessions.X!.permissionMode).toBe(null);
  });

  it("is idempotent: re-emitting the same mode is a no-op for the slot reference", () => {
    let s = reduce(initialMuxState, { type: "ensure_session", sessionId: "Y" });
    s = reduce(s, {
      type: "server_event",
      event: ev("Y", { id: 1, event: "permission_mode_changed", data: { mode: "plan" } } as any),
    });
    const slot1 = s.sessions.Y;
    s = reduce(s, {
      type: "server_event",
      event: ev("Y", { id: 2, event: "permission_mode_changed", data: { mode: "plan" } } as any),
    });
    // No identity change → mux state itself was returned unchanged too
    // (reducer's server_event branch bails when the per-session reducer
    // returns the same reference).
    expect(s.sessions.Y).toBe(slot1);
  });

  it("transitions plan → default → plan flip back the visible mode", () => {
    let s = reduce(initialMuxState, { type: "ensure_session", sessionId: "Z" });
    s = reduce(s, {
      type: "server_event",
      event: ev("Z", { id: 1, event: "permission_mode_changed", data: { mode: "plan" } } as any),
    });
    expect(s.sessions.Z!.permissionMode).toBe("plan");
    s = reduce(s, {
      type: "server_event",
      event: ev("Z", { id: 2, event: "permission_mode_changed", data: { mode: "default" } } as any),
    });
    expect(s.sessions.Z!.permissionMode).toBe("default");
    s = reduce(s, {
      type: "server_event",
      event: ev("Z", { id: 3, event: "permission_mode_changed", data: { mode: "plan" } } as any),
    });
    expect(s.sessions.Z!.permissionMode).toBe("plan");
  });

  it("does NOT affect status / messages / pending fields", () => {
    let s = reduce(initialMuxState, { type: "ensure_session", sessionId: "W" });
    // Set up some interesting state in W.
    s = reduce(s, {
      type: "server_event",
      event: ev("W", { id: 1, event: "user_message", data: { content: "hi" } } as any),
    });
    const before = s.sessions.W!;
    s = reduce(s, {
      type: "server_event",
      event: ev("W", { id: 2, event: "permission_mode_changed", data: { mode: "plan" } } as any),
    });
    const after = s.sessions.W!;
    expect(after.messages).toBe(before.messages);
    expect(after.status).toBe(before.status);
    expect(after.pendingPermission).toBe(before.pendingPermission);
    expect(after.permissionMode).toBe("plan");
  });
});
