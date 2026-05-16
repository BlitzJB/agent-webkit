/**
 * useAgentSession — error surfacing on unrecoverable stream failures
 *
 * The L1 transport handles transient drops (laptop sleep, brief network
 * blips) by reconnecting to the same session with `Last-Event-ID` so missed
 * events replay from the server's ring buffer. An error reaching the L2
 * hook means the connection is genuinely lost: the server-side session has
 * been reaped, evicted, or refuses our auth.
 *
 * We do NOT silently mint a new session in that case — doing so would hide
 * the agent-context loss from the user. Instead we surface a specific error
 * code so the UI can render a clear "session expired, start a new one"
 * affordance.
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

type ControllableSession = {
  session: Session;
  fail: (err: unknown) => void;
};

function makeControllableSession(id: string): ControllableSession {
  let rejectNext: ((err: unknown) => void) | null = null;
  let resolveNext: ((v: IteratorResult<DeliveredEvent>) => void) | null = null;

  const fail = (err: unknown): void => {
    if (rejectNext) {
      const r = rejectNext;
      rejectNext = null;
      resolveNext = null;
      r(err);
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
    detach: vi.fn(),
    close: vi.fn(async () => {}),
  } as Session;

  return { session, fail };
}

function makeClient(s: Session): { client: AgentClient; createCount: { n: number } } {
  const createCount = { n: 0 };
  const client: AgentClient = {
    async createSession(_: CreateSessionOptions | undefined) {
      createCount.n++;
      return s as Session;
    },
    attachSession(_id: string) {
      return s as Session;
    },
  };
  return { client, createCount };
}

describe("useAgentSession — unrecoverable stream errors surface with specific codes", () => {
  it("404 from the stream surfaces as code `session_not_found`", async () => {
    const a = makeControllableSession("sess-A");
    const { client, createCount } = makeClient(a.session);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );
    await waitFor(() => expect(result.current.sessionId).toBe("sess-A"));

    act(() => {
      a.fail(new TransportError("Stream /sessions/sess-A/stream failed: 404", 404));
    });

    await waitFor(() => expect(result.current.lastError).not.toBeNull());
    expect(result.current.lastError?.code).toBe("session_not_found");
    // Critically: no silent recreate. The server-side session is gone, the
    // agent context with it; hiding that would mislead the user.
    expect(createCount.n).toBe(1);
  });

  it("412 (ring-buffer cursor evicted) surfaces as code `session_evicted`", async () => {
    const a = makeControllableSession("sess-A");
    const { client, createCount } = makeClient(a.session);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );
    await waitFor(() => expect(result.current.sessionId).toBe("sess-A"));

    act(() => {
      a.fail(new TransportError("evicted", 412));
    });

    await waitFor(() => expect(result.current.lastError).not.toBeNull());
    expect(result.current.lastError?.code).toBe("session_evicted");
    expect(createCount.n).toBe(1);
  });

  it("401/403 surface as code `unauthorized`", async () => {
    const a = makeControllableSession("sess-A");
    const { client } = makeClient(a.session);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );
    await waitFor(() => expect(result.current.sessionId).toBe("sess-A"));

    act(() => {
      a.fail(new TransportError("Unauthorized", 401));
    });

    await waitFor(() => expect(result.current.lastError).not.toBeNull());
    expect(result.current.lastError?.code).toBe("unauthorized");
  });

  it("non-Transport errors keep the generic `stream_error` code", async () => {
    const a = makeControllableSession("sess-A");
    const { client } = makeClient(a.session);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );
    await waitFor(() => expect(result.current.sessionId).toBe("sess-A"));

    act(() => {
      a.fail(new Error("network unreachable"));
    });

    await waitFor(() => expect(result.current.lastError).not.toBeNull());
    expect(result.current.lastError?.code).toBe("stream_error");
  });
});
