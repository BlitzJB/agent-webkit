import { describe, it, expect } from "vitest";
import { createAgentClient } from "../src/index.js";

/**
 * In-memory fake server: a small fetch impl that emulates the wire protocol just enough
 * to verify L1 behavior. This is the L1-against-fake-server tier described in the README.
 */
function makeFakeFetch(opts: {
  events: string; // SSE body
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
    if (method === "GET" && path === "/sessions/sess-1/stream") {
      return new Response(opts.events, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    if (method === "POST" && path === "/sessions/sess-1/input") {
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

describe("Session against fake server", () => {
  it("creates a session and yields typed events ending in done", async () => {
    const events = [
      'id: 1\nevent: session_ready\ndata: {"session_id":"sess-1","protocol_version":"1.0"}\n\n',
      'id: 2\nevent: message_delta\ndata: {"message_id":"m1","delta":{"text":"Hi"}}\n\n',
      'id: 3\nevent: done\ndata: {}\n\n',
    ].join("");
    const client = createAgentClient({ baseUrl: "http://x", fetchImpl: makeFakeFetch({ events }) });
    const session = await client.createSession();
    expect(session.id).toBe("sess-1");

    const collected: { event: string; id: number }[] = [];
    for await (const ev of session.events()) {
      collected.push({ event: ev.event, id: ev.id });
    }
    expect(collected).toEqual([
      { event: "session_ready", id: 1 },
      { event: "message_delta", id: 2 },
      { event: "done", id: 3 },
    ]);
    expect(session.lastEventId).toBe("3");
  });

  it("send() POSTs the correct user_message body", async () => {
    let captured: any = null;
    const events = "id: 1\nevent: done\ndata: {}\n\n";
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch({
        events,
        onPost: (_p, body) => {
          captured = body;
          return new Response(null, { status: 204 });
        },
      }),
    });
    const session = await client.createSession();
    await session.send("hello");
    expect(captured).toEqual({ type: "user_message", content: "hello" });
  });

  it("approve() builds permission_response correctly", async () => {
    let captured: any = null;
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch({
        events: "id: 1\nevent: done\ndata: {}\n\n",
        onPost: (_p, body) => {
          captured = body;
          return new Response(null, { status: 204 });
        },
      }),
    });
    const session = await client.createSession();
    await session.approve("tu_1", { updatedInput: { foo: "bar" } });
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
    const session = await client.createSession();
    await expect(session.approve("tu_1")).rejects.toThrow(/Conflict/);
  });
});
