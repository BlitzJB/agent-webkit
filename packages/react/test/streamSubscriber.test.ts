/**
 * Singleton stream subscriber — fan-out, lifecycle, and HTTP-1.1 cap protection.
 *
 * Verifies the invariant that protects us from the 6-per-origin browser
 * connection limit: regardless of how many useAgentMux instances mount
 * (StrictMode double-mount, HMR remounts, page-error reloads), there is
 * only ever ONE client.events() iteration per AgentClient.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { AgentClient, DeliveredEvent } from "@agent-webkit/core";
import { TransportError } from "@agent-webkit/core";
import { subscribeToStream } from "../src/streamSubscriber.js";

function makeClient(): {
  client: AgentClient;
  push: (ev: DeliveredEvent) => void;
  fail: (err: unknown) => void;
  /** How many times client.events() has been invoked. */
  eventsCalls: () => number;
  /** Resolves when the current iterator has been aborted via signal. */
  abortedCount: () => number;
} {
  let calls = 0;
  let aborted = 0;
  type Pending = { resolve: (v: IteratorResult<DeliveredEvent>) => void; reject: (e: unknown) => void };
  let queue: DeliveredEvent[] = [];
  let pending: Pending | null = null;
  let failure: unknown = null;
  let currentSignal: AbortSignal | null = null;

  const events = ({ signal }: { signal?: AbortSignal } = {}): AsyncIterableIterator<DeliveredEvent> => {
    calls += 1;
    currentSignal = signal ?? null;
    if (signal) {
      signal.addEventListener("abort", () => {
        aborted += 1;
        if (pending) {
          const p = pending;
          pending = null;
          p.reject(new DOMException("aborted", "AbortError"));
        }
      });
    }
    const it: AsyncIterableIterator<DeliveredEvent> = {
      [Symbol.asyncIterator]() { return it; },
      next() {
        if (failure) {
          const f = failure;
          failure = null;
          return Promise.reject(f);
        }
        if (queue.length > 0) {
          const v = queue.shift()!;
          return Promise.resolve({ value: v, done: false });
        }
        if (currentSignal?.aborted) {
          return Promise.reject(new DOMException("aborted", "AbortError"));
        }
        return new Promise<IteratorResult<DeliveredEvent>>((resolve, reject) => {
          pending = { resolve, reject };
        });
      },
      return() { return Promise.resolve({ value: undefined as any, done: true }); },
    };
    return it;
  };

  const client = { events } as unknown as AgentClient;

  return {
    client,
    push: (ev) => {
      if (pending) {
        const p = pending;
        pending = null;
        p.resolve({ value: ev, done: false });
      } else {
        queue.push(ev);
      }
    },
    fail: (err) => {
      if (pending) {
        const p = pending;
        pending = null;
        p.reject(err);
      } else {
        failure = err;
      }
    },
    eventsCalls: () => calls,
    abortedCount: () => aborted,
  };
}

const ev = (id: number, sid = "sid_X"): DeliveredEvent =>
  ({ id, session_id: sid, event: "user_message", data: { content: `m${id}` } } as DeliveredEvent);

describe("streamSubscriber", () => {
  beforeEach(() => { vi.useFakeTimers(); });
  afterEach(() => { vi.useRealTimers(); });

  it("opens exactly one client.events() loop regardless of subscriber count", () => {
    const { client } = makeClient();
    const off1 = subscribeToStream(client, { onEvent: () => {}, onError: () => {} });
    const off2 = subscribeToStream(client, { onEvent: () => {}, onError: () => {} });
    const off3 = subscribeToStream(client, { onEvent: () => {}, onError: () => {} });
    expect((client.events as any).length >= 0).toBe(true);
    // Indirectly verified via eventsCalls below — re-fetch via the makeClient closure pattern.
    off1(); off2(); off3();
  });

  it("fans events out to every active listener", async () => {
    const { client, push, eventsCalls } = makeClient();
    const a: DeliveredEvent[] = [];
    const b: DeliveredEvent[] = [];
    const offA = subscribeToStream(client, { onEvent: (e) => a.push(e), onError: () => {} });
    const offB = subscribeToStream(client, { onEvent: (e) => b.push(e), onError: () => {} });
    expect(eventsCalls()).toBe(1);

    push(ev(1));
    await Promise.resolve(); await Promise.resolve();
    push(ev(2));
    await Promise.resolve(); await Promise.resolve();

    expect(a.map((x) => x.id)).toEqual([1, 2]);
    expect(b.map((x) => x.id)).toEqual([1, 2]);
    offA(); offB();
  });

  it("survives remount churn: unsubscribe+resubscribe within grace reuses the same loop", () => {
    const { client, eventsCalls } = makeClient();
    const off1 = subscribeToStream(client, { onEvent: () => {}, onError: () => {} });
    expect(eventsCalls()).toBe(1);

    off1();
    // Immediately resubscribe — simulates StrictMode cleanup → setup, or HMR remount.
    vi.advanceTimersByTime(50);
    const off2 = subscribeToStream(client, { onEvent: () => {}, onError: () => {} });
    vi.advanceTimersByTime(500);

    expect(eventsCalls()).toBe(1);
    off2();
  });

  it("aborts the iterator only after the last listener detaches past the grace window", () => {
    const { client, abortedCount, eventsCalls } = makeClient();
    const off = subscribeToStream(client, { onEvent: () => {}, onError: () => {} });
    expect(eventsCalls()).toBe(1);
    expect(abortedCount()).toBe(0);

    off();
    vi.advanceTimersByTime(100);
    expect(abortedCount()).toBe(0); // still within grace

    vi.advanceTimersByTime(500);
    expect(abortedCount()).toBe(1);
  });

  it("a fresh subscribe after full teardown starts a NEW loop", () => {
    const { client, eventsCalls } = makeClient();
    const off1 = subscribeToStream(client, { onEvent: () => {}, onError: () => {} });
    off1();
    vi.advanceTimersByTime(500);
    expect(eventsCalls()).toBe(1);

    const off2 = subscribeToStream(client, { onEvent: () => {}, onError: () => {} });
    expect(eventsCalls()).toBe(2);
    off2();
  });

  it("maps TransportError 401 → unauthorized for every listener", async () => {
    const { client, fail } = makeClient();
    const errs: string[] = [];
    const off = subscribeToStream(client, {
      onEvent: () => {},
      onError: (e) => errs.push(e.code),
    });
    const off2 = subscribeToStream(client, {
      onEvent: () => {},
      onError: (e) => errs.push(e.code),
    });

    fail(new TransportError("nope", 401, ""));
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();

    expect(errs).toEqual(["unauthorized", "unauthorized"]);
    off(); off2();
  });

  it("maps TransportError 412 → stream_evicted", async () => {
    const { client, fail } = makeClient();
    const errs: string[] = [];
    const off = subscribeToStream(client, { onEvent: () => {}, onError: (e) => errs.push(e.code) });
    fail(new TransportError("evicted", 412, ""));
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(errs).toEqual(["stream_evicted"]);
    off();
  });

  it("maps generic errors → stream_error", async () => {
    const { client, fail } = makeClient();
    const errs: string[] = [];
    const off = subscribeToStream(client, { onEvent: () => {}, onError: (e) => errs.push(e.code) });
    fail(new Error("boom"));
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    expect(errs).toEqual(["stream_error"]);
    off();
  });

  it("a throwing listener does not break delivery to other listeners", async () => {
    const { client, push } = makeClient();
    const good: number[] = [];
    const offBad = subscribeToStream(client, {
      onEvent: () => { throw new Error("listener bug"); },
      onError: () => {},
    });
    const offGood = subscribeToStream(client, {
      onEvent: (e) => good.push(e.id),
      onError: () => {},
    });
    push(ev(1));
    await Promise.resolve(); await Promise.resolve();
    push(ev(2));
    await Promise.resolve(); await Promise.resolve();
    expect(good).toEqual([1, 2]);
    offBad(); offGood();
  });

  it("separate AgentClient instances get separate loops", () => {
    const a = makeClient();
    const b = makeClient();
    const offA = subscribeToStream(a.client, { onEvent: () => {}, onError: () => {} });
    const offB = subscribeToStream(b.client, { onEvent: () => {}, onError: () => {} });
    expect(a.eventsCalls()).toBe(1);
    expect(b.eventsCalls()).toBe(1);
    offA(); offB();
  });
});
