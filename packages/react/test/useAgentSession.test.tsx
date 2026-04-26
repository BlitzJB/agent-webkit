/**
 * Hook-level integration tests for useAgentSession.
 *
 * We inject a fake AgentClient instead of hitting a server — the hook's contract is:
 *   1. Open one session per mount (idempotent under StrictMode double-invoke).
 *   2. Reconcile streamed events into the reducer state.
 *   3. On unmount: detach (abort SSE) but DO NOT delete the server session.
 *   4. close() is the explicit destroy and SHOULD call DELETE.
 *
 * @vitest-environment happy-dom
 */
import { describe, it, expect, vi } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import type {
  AgentClient,
  CreateSessionOptions,
  DeliveredEvent,
  Session,
} from "@agent-webkit/core";
import { useAgentSession } from "../src/useAgentSession.js";

function makeFakeSession(id = "sess-1"): {
  session: Session;
  push: (ev: DeliveredEvent) => void;
  endStream: () => void;
  closed: { value: boolean };
  detached: { value: boolean };
} {
  const queue: DeliveredEvent[] = [];
  let resolveNext: ((v: IteratorResult<DeliveredEvent>) => void) | null = null;
  let ended = false;
  const closed = { value: false };
  const detached = { value: false };

  const push = (ev: DeliveredEvent): void => {
    if (resolveNext) {
      const r = resolveNext;
      resolveNext = null;
      r({ value: ev, done: false });
    } else {
      queue.push(ev);
    }
  };
  const endStream = (): void => {
    ended = true;
    if (resolveNext) {
      const r = resolveNext;
      resolveNext = null;
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
              if (ended) return Promise.resolve({ value: undefined as any, done: true });
              return new Promise((resolve) => {
                resolveNext = resolve;
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
    detach: vi.fn(() => {
      detached.value = true;
      endStream();
    }),
    close: vi.fn(async () => {
      closed.value = true;
      endStream();
    }),
  } as Session;

  return { session, push, endStream, closed, detached };
}

function makeFakeClient(session: Session): { client: AgentClient; createCount: { n: number } } {
  const createCount = { n: 0 };
  const client: AgentClient = {
    async createSession(_: CreateSessionOptions | undefined) {
      createCount.n++;
      return session as Session;
    },
    attachSession(_id: string) {
      return session as Session;
    },
  };
  return { client, createCount };
}

describe("useAgentSession", () => {
  it("creates a session on mount and exposes its id", async () => {
    const { session } = makeFakeSession();
    const { client, createCount } = makeFakeClient(session);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );

    await waitFor(() => {
      expect(result.current.sessionId).toBe("sess-1");
    });
    expect(createCount.n).toBe(1);
  });

  it("reconciles streamed assistant message into messages state", async () => {
    const { session, push } = makeFakeSession();
    const { client } = makeFakeClient(session);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );

    await waitFor(() => expect(result.current.sessionId).toBe("sess-1"));

    act(() => {
      push({
        id: 1,
        event: "session_ready",
        data: { session_id: "sess-1", protocol_version: "1.0" },
      } as DeliveredEvent);
      push({
        id: 2,
        event: "message_complete",
        data: {
          message_id: "m_1",
          message: {
            id: "m_1",
            role: "assistant",
            content: [{ type: "text", text: "hello" }],
          },
        },
      } as DeliveredEvent);
    });

    await waitFor(() => {
      const assistant = result.current.messages.find((m) => m.kind === "assistant");
      expect(assistant).toBeTruthy();
      expect(JSON.stringify(assistant)).toContain("hello");
    });
  });

  it("on unmount: calls detach() and does NOT call close() (StrictMode safe)", async () => {
    const { session, detached, closed } = makeFakeSession();
    const { client } = makeFakeClient(session);

    const { result, unmount } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );
    await waitFor(() => expect(result.current.sessionId).toBe("sess-1"));

    unmount();

    expect(detached.value).toBe(true);
    expect(closed.value).toBe(false);
    expect(session.detach).toHaveBeenCalled();
    expect(session.close).not.toHaveBeenCalled();
  });

  it("StrictMode double-mount does not destroy the session", async () => {
    const { session, closed } = makeFakeSession();
    const { client } = makeFakeClient(session);

    const { result } = renderHook(
      () => useAgentSession({ baseUrl: "http://test", client }),
      { wrapper: StrictMode }
    );
    await waitFor(() => expect(result.current.sessionId).toBe("sess-1"));

    // StrictMode runs effect → cleanup → effect synchronously in dev. close() must not fire.
    expect(closed.value).toBe(false);
    expect(session.close).not.toHaveBeenCalled();
  });

  it("explicit close() invokes the underlying session.close (DELETE)", async () => {
    const { session, closed } = makeFakeSession();
    const { client } = makeFakeClient(session);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );
    await waitFor(() => expect(result.current.sessionId).toBe("sess-1"));

    await act(async () => {
      await result.current.close();
    });

    expect(closed.value).toBe(true);
    expect(session.close).toHaveBeenCalled();
  });

  it("send() forwards to the underlying session", async () => {
    const { session } = makeFakeSession();
    const { client } = makeFakeClient(session);

    const { result } = renderHook(() =>
      useAgentSession({ baseUrl: "http://test", client })
    );
    await waitFor(() => expect(result.current.sessionId).toBe("sess-1"));

    await act(async () => {
      await result.current.send("hi");
    });
    expect(session.send).toHaveBeenCalledWith("hi");
  });
});
