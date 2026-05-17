import { describe, it, expect } from "vitest";
import { createAgentClient, type AgentClient } from "../src/index.js";

// The production `events()` never terminates cleanly — it auto-reconnects on
// EOF since /stream is meant to stay open forever. For tests, we collect
// until a stopping condition then abort.
async function collectUntil(
  client: AgentClient,
  stop: (e: Awaited<ReturnType<AgentClient["events"]>> extends AsyncIterable<infer T> ? T : never) => boolean,
  timeoutMs = 2000,
): Promise<any[]> {
  const ac = new AbortController();
  const collected: any[] = [];
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    for await (const ev of client.events({ signal: ac.signal })) {
      collected.push(ev);
      if (stop(ev as any)) {
        ac.abort();
        break;
      }
    }
  } catch {
    /* abort */
  } finally {
    clearTimeout(timer);
  }
  return collected;
}

/**
 * In-memory fake server: a small fetch impl that emulates the multiplexed
 * wire protocol. One persistent /stream, per-session POST /input, GET /history
 * for past events, GET /sessions for list, POST /sessions for create.
 */
function makeFakeFetch(opts: {
  events: string; // SSE body for /stream
  onPost?: (path: string, body: unknown) => Response | Promise<Response>;
}): typeof fetch {
  const fakeFetch: typeof fetch = async (input, init) => {
    const url = typeof input === "string" ? input : (input as URL).toString();
    const path = new URL(url).pathname;
    const method = init?.method ?? "GET";

    if (method === "POST" && path === "/sessions") {
      return new Response(
        JSON.stringify({ session_id: "sess-1", protocol_version: "1.0" }),
        { status: 200, headers: { "content-type": "application/json" } }
      );
    }
    if (method === "GET" && path === "/stream") {
      return new Response(opts.events, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    if (method === "POST" && path.startsWith("/sessions/") && path.endsWith("/input")) {
      const body = init?.body ? JSON.parse(init.body as string) : null;
      if (opts.onPost) return opts.onPost(path, body);
      return new Response(null, { status: 204 });
    }
    if (method === "DELETE" && path === "/sessions/sess-1") {
      return new Response(null, { status: 204 });
    }
    return new Response("not found", { status: 404 });
  };
  return fakeFetch;
}

// Build a multiplexed SSE frame in the wire envelope shape.
function envelopeFrame(seq: number, eventName: string, sessionId: string, payload: unknown): string {
  return `id: ${seq}\nevent: ${eventName}\ndata: ${JSON.stringify({ session_id: sessionId, payload })}\n\n`;
}

describe("AgentClient against fake server", () => {
  it("createSession returns id; events() yields envelope events with session_id", async () => {
    const events =
      envelopeFrame(1, "session_ready", "sess-1", {
        session_id: "sess-1",
        protocol_version: "1.0",
      }) +
      envelopeFrame(2, "message_delta", "sess-1", { message_id: "m1", delta: { text: "Hi" } }) +
      envelopeFrame(3, "done", "sess-1", {});

    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch({ events }),
    });
    const created = await client.createSession();
    expect(created.session_id).toBe("sess-1");

    const collected = await collectUntil(client, (ev) => ev.event === "done");
    expect(collected.map((e) => ({ event: e.event, sid: e.session_id, id: e.id }))).toEqual([
      { event: "session_ready", sid: "sess-1", id: 1 },
      { event: "message_delta", sid: "sess-1", id: 2 },
      { event: "done", sid: "sess-1", id: 3 },
    ]);
    expect(client.lastEventId).toBe("3");
  });

  it("send() POSTs the correct user_message body to /sessions/{id}/input", async () => {
    let captured: any = null;
    let capturedPath = "";
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch({
        events: envelopeFrame(1, "done", "sess-1", {}),
        onPost: (path, body) => {
          capturedPath = path;
          captured = body;
          return new Response(null, { status: 204 });
        },
      }),
    });
    await client.send("sess-1", "hello");
    expect(captured).toEqual({ type: "user_message", content: "hello" });
    expect(capturedPath).toBe("/sessions/sess-1/input");
  });

  it("approve() builds the right permission_response body for the right session", async () => {
    let captured: any = null;
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch({
        events: envelopeFrame(1, "done", "sess-1", {}),
        onPost: (_p, body) => {
          captured = body;
          return new Response(null, { status: 204 });
        },
      }),
    });
    await client.approve("sess-1", "tu_1", { updatedInput: { foo: "bar" } });
    expect(captured).toEqual({
      type: "permission_response",
      correlation_id: "tu_1",
      behavior: "allow",
      updated_input: { foo: "bar" },
    });
  });

  it("409 from input throws a TransportError", async () => {
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch({
        events: "",
        onPost: () => new Response("conflict", { status: 409 }),
      }),
    });
    await expect(client.approve("sess-1", "tu_1")).rejects.toThrow(/Conflict/);
  });

  it("events() multiplexes across sessions — session_id distinguishes each frame", async () => {
    const events =
      envelopeFrame(1, "message_complete", "sess-A", {
        message_id: "mA",
        message: { id: "mA", role: "assistant", content: [{ type: "text", text: "from A" }] },
      }) +
      envelopeFrame(2, "message_complete", "sess-B", {
        message_id: "mB",
        message: { id: "mB", role: "assistant", content: [{ type: "text", text: "from B" }] },
      });
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch({ events }),
    });
    const collected = await collectUntil(
      client,
      (ev) => ev.session_id === "sess-B" && ev.event === "message_complete"
    );
    expect(collected.map((e) => e.session_id)).toEqual(["sess-A", "sess-B"]);
  });
});
