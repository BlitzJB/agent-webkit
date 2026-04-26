import { feedSSE, newSSEParserState, type ParsedSSEEvent } from "./sse.js";

export interface TransportOptions {
  baseUrl: string;
  token?: string | undefined;
  fetchImpl?: typeof fetch | undefined;
}

/**
 * Low-level transport: POST helpers + an async iterable SSE reader with auto-reconnect.
 * Aware of `Last-Event-ID` for resume.
 */
export class Transport {
  private readonly baseUrl: string;
  private readonly token: string | undefined;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: TransportOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.token = opts.token;
    this.fetchImpl = opts.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  private headers(extra?: Record<string, string>): Record<string, string> {
    const h: Record<string, string> = { "content-type": "application/json", ...extra };
    if (this.token) h["authorization"] = `Bearer ${this.token}`;
    return h;
  }

  async post(path: string, body: unknown): Promise<Response> {
    const res = await this.fetchImpl(this.baseUrl + path, {
      method: "POST",
      headers: this.headers(),
      body: JSON.stringify(body),
    });
    return res;
  }

  async postJSON<T>(path: string, body: unknown): Promise<T> {
    const res = await this.post(path, body);
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new TransportError(`POST ${path} failed: ${res.status}`, res.status, text);
    }
    return (await res.json()) as T;
  }

  async delete(path: string): Promise<void> {
    const res = await this.fetchImpl(this.baseUrl + path, {
      method: "DELETE",
      headers: this.headers(),
    });
    if (!res.ok && res.status !== 404) {
      throw new TransportError(`DELETE ${path} failed: ${res.status}`, res.status);
    }
  }

  /**
   * Yields SSE events for a session stream. Auto-reconnects on transient errors,
   * resuming from the last seen `id` via the `Last-Event-ID` header.
   *
   * Caller can stop by breaking out of the loop (the AbortController is wired up via signal).
   */
  async *streamEvents(
    path: string,
    opts?: { signal?: AbortSignal; lastEventId?: string }
  ): AsyncGenerator<ParsedSSEEvent, void, void> {
    let lastId = opts?.lastEventId;
    let backoff = 250;
    const MAX_BACKOFF = 8000;

    while (true) {
      if (opts?.signal?.aborted) return;

      const headers = this.headers({ accept: "text/event-stream" });
      if (lastId) headers["last-event-id"] = lastId;

      let res: Response;
      try {
        const init: RequestInit = { method: "GET", headers };
        if (opts?.signal) init.signal = opts.signal;
        res = await this.fetchImpl(this.baseUrl + path, init);
      } catch (err) {
        if (opts?.signal?.aborted) return;
        await sleep(backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF);
        continue;
      }

      if (res.status === 412) {
        throw new TransportError(
          `Server has evicted Last-Event-ID=${lastId} from its ring buffer; cannot resume.`,
          412
        );
      }
      if (!res.ok || !res.body) {
        if (res.status >= 400 && res.status < 500 && res.status !== 408 && res.status !== 429) {
          throw new TransportError(`Stream ${path} failed: ${res.status}`, res.status);
        }
        await sleep(backoff);
        backoff = Math.min(backoff * 2, MAX_BACKOFF);
        continue;
      }

      backoff = 250; // reset on a successful connection
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      const state = newSSEParserState();

      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          const events = feedSSE(state, decoder.decode(value, { stream: true }));
          for (const ev of events) {
            if (ev.id !== undefined) lastId = ev.id;
            yield ev;
          }
        }
      } catch (err) {
        if (opts?.signal?.aborted) return;
        // fall through to reconnect
      } finally {
        try {
          reader.releaseLock();
        } catch {
          /* noop */
        }
      }

      if (opts?.signal?.aborted) return;
      await sleep(backoff);
    }
  }
}

export class TransportError extends Error {
  readonly status: number | undefined;
  readonly body: string | undefined;
  constructor(message: string, status?: number, body?: string) {
    super(message);
    this.name = "TransportError";
    this.status = status;
    this.body = body;
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}
