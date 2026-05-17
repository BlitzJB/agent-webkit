/**
 * L1 streaming tests — verifies that `message_delta` events flow through the
 * SSE parser, Transport reader, and AgentClient.events() in chunk-boundary-safe
 * order, with the multiplex envelope unwrapped.
 *
 * The L1 SDK has no "message accumulator" — it just emits typed events tagged
 * with session_id — so these tests live at the wire level: parser correctness
 * across chunked deltas, transport ordering, and protocol typing for text +
 * input_json_delta.
 */
import { describe, it, expect } from "vitest";
import { createAgentClient, type AgentClient } from "../src/index.js";
import { feedSSE, newSSEParserState } from "../src/sse.js";
import type { DeliveredEvent } from "../src/types.js";

async function collectUntil(
  client: AgentClient,
  stop: (e: DeliveredEvent) => boolean,
  timeoutMs = 2000,
): Promise<DeliveredEvent[]> {
  const ac = new AbortController();
  const out: DeliveredEvent[] = [];
  const timer = setTimeout(() => ac.abort(), timeoutMs);
  try {
    for await (const ev of client.events({ signal: ac.signal })) {
      out.push(ev);
      if (stop(ev)) {
        ac.abort();
        break;
      }
    }
  } catch {
    /* abort */
  } finally {
    clearTimeout(timer);
  }
  return out;
}

function makeFakeFetch(events: string): typeof fetch {
  return async (input, init) => {
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
      return new Response(events, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    if (method === "POST" && path.startsWith("/sessions/") && path.endsWith("/input")) {
      return new Response(null, { status: 204 });
    }
    if (method === "DELETE") return new Response(null, { status: 204 });
    return new Response("not found", { status: 404 });
  };
}

// Multiplex envelope frame on the wire.
function frame(seq: number, eventName: string, sessionId: string, payload: unknown): string {
  return `id: ${seq}\nevent: ${eventName}\ndata: ${JSON.stringify({ session_id: sessionId, payload })}\n\n`;
}

// Build a multiplexed wire stream that delivers many text deltas + final
// message_complete + result for a given session.
function streamingTextWire(sessionId: string, messageId: string, tokens: string[]): string {
  const parts: string[] = [
    frame(1, "session_ready", sessionId, { session_id: sessionId, protocol_version: "1.0" }),
  ];
  tokens.forEach((tok, i) => {
    parts.push(
      frame(i + 2, "message_delta", sessionId, {
        message_id: messageId,
        delta: { type: "text", text: tok },
      })
    );
  });
  const full = tokens.join("");
  parts.push(
    frame(tokens.length + 2, "message_complete", sessionId, {
      message_id: messageId,
      message: { id: messageId, role: "assistant", content: [{ type: "text", text: full }] },
    })
  );
  parts.push(
    frame(tokens.length + 3, "result", sessionId, {
      session_id: sessionId,
      subtype: "success",
      total_cost_usd: 0.01,
    })
  );
  parts.push(frame(tokens.length + 4, "done", sessionId, {}));
  return parts.join("");
}

describe("L1 streaming — message_delta over AgentClient.events()", () => {
  it("yields every delta in order followed by message_complete, tagged with session_id", async () => {
    const tokens = ["Hel", "lo", " ", "world"];
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch(streamingTextWire("sess-1", "m1", tokens)),
    });
    await client.createSession();

    const collected = await collectUntil(client, (ev) => ev.event === "done");
    const observed: { event: string; sid: string; preview?: string }[] = collected.map((ev) => {
      if (ev.event === "message_delta") {
        const d = ev.data as { delta: { text?: string } };
        return { event: "message_delta", sid: ev.session_id, preview: d.delta.text };
      }
      if (ev.event === "message_complete") {
        const d = ev.data as { message: { content: Array<{ text?: string }> } };
        return { event: "message_complete", sid: ev.session_id, preview: d.message.content[0]?.text };
      }
      return { event: ev.event, sid: ev.session_id };
    });

    expect(observed).toEqual([
      { event: "session_ready", sid: "sess-1" },
      { event: "message_delta", sid: "sess-1", preview: "Hel" },
      { event: "message_delta", sid: "sess-1", preview: "lo" },
      { event: "message_delta", sid: "sess-1", preview: " " },
      { event: "message_delta", sid: "sess-1", preview: "world" },
      { event: "message_complete", sid: "sess-1", preview: "Hello world" },
      { event: "result", sid: "sess-1" },
      { event: "done", sid: "sess-1" },
    ]);
  });

  it("typed delta payload exposes text and message_id", async () => {
    const wire = streamingTextWire("sess-1", "m_typed", ["A", "B"]);
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch(wire),
    });
    await client.createSession();
    const collected = await collectUntil(client, (ev) => ev.event === "done");
    const deltas = collected.filter((e) => e.event === "message_delta");
    expect(deltas).toHaveLength(2);
    for (const d of deltas) {
      const data = d.data as { message_id: string; delta: { text?: string } };
      expect(data.message_id).toBe("m_typed");
      expect(typeof data.delta.text).toBe("string");
    }
  });

  it("client.lastEventId advances to the last delivered delta id", async () => {
    const wire = streamingTextWire("sess-1", "m_id", ["x", "y", "z"]);
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch(wire),
    });
    await client.createSession();
    // tokens=3 → session_ready(1) + 3 deltas + complete + result + done = id 7
    const collected = await collectUntil(client, (ev) => ev.event === "done");
    expect(client.lastEventId).toBe(String(collected[collected.length - 1]!.id));
    expect(client.lastEventId).toBe("7");
  });

  it("forwards input_json_delta deltas verbatim (for GenUI buffering)", async () => {
    const wire = [
      frame(1, "session_ready", "sess-1", { session_id: "sess-1", protocol_version: "1.0" }),
      frame(2, "message_delta", "sess-1", {
        message_id: "m_gen",
        delta: {
          type: "input_json_delta",
          partial_json: '{"location":',
          tool_use_id: "tu_42",
          name: "mcp__genui__render_weather_card",
        },
      }),
      frame(3, "message_delta", "sess-1", {
        message_id: "m_gen",
        delta: {
          type: "input_json_delta",
          partial_json: '"Boston"}',
          tool_use_id: "tu_42",
        },
      }),
      frame(4, "done", "sess-1", {}),
    ].join("");
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch(wire),
    });
    await client.createSession();
    const collected = await collectUntil(client, (ev) => ev.event === "done");
    const deltas = collected
      .filter((ev) => ev.event === "message_delta")
      .map((ev) => (ev.data as { delta: Record<string, unknown> }).delta);
    expect(deltas).toEqual([
      {
        type: "input_json_delta",
        partial_json: '{"location":',
        tool_use_id: "tu_42",
        name: "mcp__genui__render_weather_card",
      },
      {
        type: "input_json_delta",
        partial_json: '"Boston"}',
        tool_use_id: "tu_42",
      },
    ]);
  });
});

describe("L1 streaming — SSE parser robustness across fragmented chunks", () => {
  it("emits a delta even when the JSON arrives split across chunk boundaries", () => {
    const s = newSSEParserState();
    const a = feedSSE(s, 'id: 2\nevent: message_delta\ndata: {"message_id":"m1","delta":{"');
    const b = feedSSE(s, 'type":"text","text":"hello"}}\n');
    const c = feedSSE(s, "\n");
    expect(a).toEqual([]);
    expect(b).toEqual([]);
    expect(c).toHaveLength(1);
    const ev = c[0]!;
    expect(ev.event).toBe("message_delta");
    expect(ev.id).toBe("2");
    expect(JSON.parse(ev.data)).toEqual({
      message_id: "m1",
      delta: { type: "text", text: "hello" },
    });
  });

  it("delivers many small deltas split character-by-character without losing any", () => {
    const s = newSSEParserState();
    const tokens = ["A", "B", "C", "D"];
    const wire =
      tokens
        .map(
          (t, i) =>
            `id: ${i + 1}\nevent: message_delta\ndata: ${JSON.stringify({
              message_id: "m",
              delta: { type: "text", text: t },
            })}\n\n`
        )
        .join("");
    const out: ReturnType<typeof feedSSE> = [];
    for (const ch of wire) out.push(...feedSSE(s, ch));
    expect(out).toHaveLength(4);
    expect(out.map((e) => e.id)).toEqual(["1", "2", "3", "4"]);
    for (let i = 0; i < 4; i++) {
      expect(JSON.parse(out[i]!.data)).toEqual({
        message_id: "m",
        delta: { type: "text", text: tokens[i] },
      });
    }
  });

  it("a comment line interleaved between deltas does not break the next dispatch", () => {
    const s = newSSEParserState();
    const data = `${frame(1, "x", "s", { v: 1 })}: keepalive\n\n${frame(2, "x", "s", { v: 2 })}`;
    const out = feedSSE(s, data);
    expect(out.map((e) => e.id)).toEqual(["1", "2"]);
  });
});
