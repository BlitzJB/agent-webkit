import type { AgentClient, DeliveredEvent } from "@agent-webkit/core";
import { TransportError } from "@agent-webkit/core";

// ────────────────────────────────────────────────────────────────────────────
// Module-level /stream subscriber.
//
// One client.events() iteration per AgentClient instance, fanned out to N
// hook instances. Survives component remounts (StrictMode double-mount, HMR,
// page-error reloads), so we never accumulate browser-side SSE sockets past
// the HTTP/1.1 6-per-origin cap.
//
// A subscriber is created lazily on first subscribe() and torn down only
// when the last listener detaches (after a short grace window so a remount
// doesn't churn the connection).
// ────────────────────────────────────────────────────────────────────────────

export interface StreamError {
  code: "stream_error" | "unauthorized" | "stream_evicted";
  message: string;
}

export interface StreamListener {
  onEvent: (ev: DeliveredEvent) => void;
  onError: (err: StreamError) => void;
}

interface Subscriber {
  listeners: Set<StreamListener>;
  abort: AbortController;
  teardownTimer: ReturnType<typeof setTimeout> | null;
}

const subscribers = new WeakMap<AgentClient, Subscriber>();
const TEARDOWN_GRACE_MS = 250;

function start(client: AgentClient): Subscriber {
  const ac = new AbortController();
  const sub: Subscriber = {
    listeners: new Set(),
    abort: ac,
    teardownTimer: null,
  };

  (async () => {
    try {
      for await (const ev of client.events({ signal: ac.signal })) {
        if (ac.signal.aborted) break;
        for (const l of sub.listeners) {
          try { l.onEvent(ev); } catch { /* listeners must not break the loop */ }
        }
      }
    } catch (err) {
      if (ac.signal.aborted) return;
      const code: StreamError["code"] =
        err instanceof TransportError && err.status === 401 ? "unauthorized"
        : err instanceof TransportError && err.status === 412 ? "stream_evicted"
        : "stream_error";
      const error: StreamError = {
        code,
        message: err instanceof Error ? err.message : String(err),
      };
      for (const l of sub.listeners) {
        try { l.onError(error); } catch { /* swallow */ }
      }
    }
  })();

  return sub;
}

export function subscribeToStream(client: AgentClient, listener: StreamListener): () => void {
  let sub = subscribers.get(client);
  if (!sub) {
    sub = start(client);
    subscribers.set(client, sub);
  } else if (sub.teardownTimer !== null) {
    clearTimeout(sub.teardownTimer);
    sub.teardownTimer = null;
  }
  sub.listeners.add(listener);

  return () => {
    const s = subscribers.get(client);
    if (!s) return;
    s.listeners.delete(listener);
    if (s.listeners.size === 0 && s.teardownTimer === null) {
      s.teardownTimer = setTimeout(() => {
        const cur = subscribers.get(client);
        if (cur && cur.listeners.size === 0) {
          cur.abort.abort();
          subscribers.delete(client);
        }
      }, TEARDOWN_GRACE_MS);
    }
  };
}
