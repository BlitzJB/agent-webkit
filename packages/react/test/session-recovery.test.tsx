/**
 * useAgentSession — auto-recovery on session loss
 *
 * Server-side sessions are reaped after idle (default 5 min) and any
 * subsequent /stream request returns 404. When the L1 transport surfaces
 * that as a `TransportError(status=404)`, L2 should silently create a fresh
 * session and continue, *as long as* the caller didn't explicitly attach to
 * a specific sessionId (in which case lifecycle is theirs to manage).
 *
 * This is the laptop-wake / dev-server-restart scenario.
 *
 * @vitest-environment happy-dom
 */
import { describe, it, expect, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import type {
  AgentClient,
  CreateSessionOptions,
  DeliveredEvent,
  Session,
} from "@agent-webkit/core";
import { TransportError } from "@agent-webkit/core";
import { useAgentSession } from "../src/useAgentSession.js";

// ---------- helpers ----------

type ControllableSession = {
  session: Session;
  push: (ev: DeliveredEvent) => void;
  fail: (err: unknown) => void;
  endStream: () => void;
};

function makeControllableSession(id: string): ControllableSession {
  const queue: DeliveredEvent[] = [];
  let resolveNext:
    | ((v: IteratorResult<DeliveredEvent>) => void)
    | null = null;
  let rejectNext: ((err: unknown) => void) | null = null;
  let ended = false;

  const push = (ev: DeliveredEvent): void => {
    if (resolveNext) {
      const r = resolveNext;
      resolveNext = null;
      rejectNext = null;
      r({ value: ev, done: false });
    } else {
      queue.push(ev);
    }
  };

  const fail = (err: unknown): void => {
    if (rejectNext) {
      const r = rejectNext;
      rejectNext = null;
      resolveNext = null;
      r(err);
    }
  };

  const endStream = (): void => {
    ended = true;
    if (resolveNext) {
      const r = resolveNext;
      resolveNext = null;
      rejectNext = null;
      r({ value: undefined as unknown as DeliveredEvent, done: true });
    }
  };

  const session: Session = {
    id,
    protocolVersion: "1.0",
    lastEventId: undefined,
    events(): AsyncIterable<DeliveredEvent> {
      return {
        [Symbol.asyncIterator](): AsyncIterator<DeliveredEvent> {
          return {
            next(): Promise<IteratorResult<DeliveredEvent>> {
              if (queue.length > 0) {
                return Promise.resolve({ value: queue.shift()!, done: false });
              }
              if (ended) {
                return Promise.resolve({ value: undefined as any, done: true });
              }
              return new Promise((resolve, reject) => {
                resolveNext = resolve;
                rejectNext = reject;
              });
            },
          };
        },
      };
    },
    send: vi.fn(async () => {}),
    interrupt: vi.fn(async () => {}),
    approve: vi.fn(async () => {}),
    deny: vi.fn(async () => {}),
    answer: vi.fn(async () => {}),
    setPermissionMode: vi.fn(async () => {}),
    setModel: vi.fn(async () => {}),
    stopTask: vi.fn(async () => {}),
    detach: vi.fn(() => endStream()),
    close: vi.fn(async () => endStream()),
  } as Session;

  return { session, push, fail, endStream };
}

function makeRecoveringClient(sessions: ControllableSession[]): {
  client: AgentClient;
  createCount: { n: number };
  attachCount: { n: number };
} {
  const createCount = { n: 0 };
  const attachCount = { n: 0 };
  const client: AgentClient = {
    async createSession(_: CreateSessionOptions | undefined) {
      const s = sessions[createCount.n] ?? sessions[sessions.length - 1];
      createCount.n++;
      return s!.session as Session;
    },
    attachSession(_id: string) {
      attachCount.n++;
      return (sessions[0]!.session) as Session;
    },
  };
  return { client, createCount, attachCount };
}

// ---------- tests ----------

describe("useAgentSession — auto-recovery", () => {
  it("recreates the session when the stream fails with 404", async () => {
    const a = makeControllableSession("sess-A");
    const b = makeControllableSession("sess-B");
    const { client, createCount } = makeRecoveringClient([a, b]);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );

    await waitFor(() => expect(result.current.sessionId).toBe("sess-A"));
    expect(createCount.n).toBe(1);

    // Server reaped the session — stream returns 404.
    act(() => {
      a.fail(new TransportError("Stream /sessions/sess-A/stream failed: 404", 404));
    });

    // L2 should silently recreate.
    await waitFor(() => expect(result.current.sessionId).toBe("sess-B"));
    expect(createCount.n).toBe(2);
    // No error surfaced — the recovery is transparent.
    expect(result.current.lastError).toBeNull();
  });

  it("recreates on 412 (ring-buffer evicted) too", async () => {
    const a = makeControllableSession("sess-A");
    const b = makeControllableSession("sess-B");
    const { client, createCount } = makeRecoveringClient([a, b]);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );

    await waitFor(() => expect(result.current.sessionId).toBe("sess-A"));

    act(() => {
      a.fail(new TransportError("evicted", 412));
    });

    await waitFor(() => expect(result.current.sessionId).toBe("sess-B"));
    expect(createCount.n).toBe(2);
  });

  it("does NOT recreate when the caller attached to an explicit sessionId", async () => {
    // attachSession lifecycle is owned by the caller; we surface the error
    // instead of silently swapping in a fresh server session.
    const a = makeControllableSession("sess-attached");
    const { client, createCount } = makeRecoveringClient([a, a]);

    const { result } = renderHook(() =>
      useAgentSession({
        baseUrl: "http://test",
        client,
        sessionId: "sess-attached",
      })
    );

    await waitFor(() => expect(result.current.sessionId).toBe("sess-attached"));

    act(() => {
      a.fail(new TransportError("404", 404));
    });

    await waitFor(() => expect(result.current.lastError).not.toBeNull());
    expect(createCount.n).toBe(0);
    expect(result.current.lastError?.code).toMatch(/stream|recover/i);
  });

  it("does NOT recreate on non-recoverable errors (e.g. 401)", async () => {
    // 401 is an auth/config failure — recreating won't help.
    const a = makeControllableSession("sess-A");
    const { client, createCount } = makeRecoveringClient([a, a]);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );

    await waitFor(() => expect(result.current.sessionId).toBe("sess-A"));

    act(() => {
      a.fail(new TransportError("Unauthorized", 401));
    });

    await waitFor(() => expect(result.current.lastError).not.toBeNull());
    expect(createCount.n).toBe(1);
  });

  it("can be opted out via autoRecover: false", async () => {
    const a = makeControllableSession("sess-A");
    const { client, createCount } = makeRecoveringClient([a, a]);

    const { result } = renderHook(() =>
      useAgentSession({
        baseUrl: "http://test",
        client,
        autoRecover: false,
      })
    );

    await waitFor(() => expect(result.current.sessionId).toBe("sess-A"));

    act(() => {
      a.fail(new TransportError("404", 404));
    });

    await waitFor(() => expect(result.current.lastError).not.toBeNull());
    expect(createCount.n).toBe(1);
  });

  it("preserves existing reducer state across recovery", async () => {
    // Messages we'd accumulated against session A must survive the swap to
    // session B — the UX expectation is "I just kept talking" even though
    // the server agent context has reset.
    const a = makeControllableSession("sess-A");
    const b = makeControllableSession("sess-B");
    const { client } = makeRecoveringClient([a, b]);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );

    await waitFor(() => expect(result.current.sessionId).toBe("sess-A"));

    act(() => {
      a.push({
        id: 1,
        event: "message_complete",
        data: {
          message_id: "m1",
          message: {
            id: "m1",
            role: "assistant",
            content: [{ type: "text", text: "hello" }],
          },
        },
      });
    });

    await waitFor(() => expect(result.current.messages.length).toBe(1));

    act(() => {
      a.fail(new TransportError("404", 404));
    });

    await waitFor(() => expect(result.current.sessionId).toBe("sess-B"));
    // Messages array untouched by the recovery.
    expect(result.current.messages.length).toBe(1);
  });
});
