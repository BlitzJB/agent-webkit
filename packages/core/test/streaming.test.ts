/**
 * L1 streaming tests — verifies that `message_delta` events flow through the
 * SSE parser, Transport reader, and Session.events() in chunk-boundary-safe
 * order. The L1 SDK has no "message accumulator" — it just emits typed events
 * — so these tests live at the wire level: parser correctness across chunked
 * deltas, transport ordering, and protocol typing for text + input_json_delta.
 */
import { describe, it, expect } from "vitest";
import { createAgentClient } from "../src/index.js";
import { feedSSE, newSSEParserState } from "../src/sse.js";
import type { DeliveredEvent } from "../src/types.js";

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
    if (method === "GET" && path === "/sessions/sess-1/stream") {
      return new Response(events, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    }
    if (method === "POST" && path === "/sessions/sess-1/input") {
      return new Response(null, { status: 204 });
    }
    if (method === "DELETE") return new Response(null, { status: 204 });
    return new Response("not found", { status: 404 });
  };
}

// Build a chunked SSE stream that delivers many small text deltas followed by
// the final message_complete and a result frame.
function streamingTextWire(messageId: string, tokens: string[]): string {
  const parts: string[] = [
    'id: 1\nevent: session_ready\ndata: {"session_id":"sess-1","protocol_version":"1.0"}\n\n',
  ];
  tokens.forEach((tok, i) => {
    parts.push(
      `id: ${i + 2}\nevent: message_delta\ndata: ${JSON.stringify({
        message_id: messageId,
        delta: { type: "text", text: tok },
      })}\n\n`
    );
  });
  const full = tokens.join("");
  parts.push(
    `id: ${tokens.length + 2}\nevent: message_complete\ndata: ${JSON.stringify({
      message_id: messageId,
      message: {
        id: messageId,
        role: "assistant",
        content: [{ type: "text", text: full }],
      },
    })}\n\n`
  );
  parts.push(
    `id: ${tokens.length + 3}\nevent: result\ndata: {"session_id":"sess-1","subtype":"success","total_cost_usd":0.01}\n\n`
  );
  parts.push(`id: ${tokens.length + 4}\nevent: done\ndata: {}\n\n`);
  return parts.join("");
}

describe("L1 streaming — message_delta over Session.events()", () => {
  it("yields every delta in order followed by message_complete", async () => {
    const tokens = ["Hel", "lo", " ", "world"];
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch(streamingTextWire("m1", tokens)),
    });
    const session = await client.createSession();

    const observed: { event: string; preview?: string }[] = [];
    for await (const ev of session.events()) {
      if (ev.event === "message_delta") {
        const d = ev.data as { delta: { text?: string } };
        observed.push({ event: "message_delta", preview: d.delta.text });
      } else if (ev.event === "message_complete") {
        const d = ev.data as { message: { content: Array<{ text?: string }> } };
        observed.push({ event: "message_complete", preview: d.message.content[0]?.text });
      } else {
        observed.push({ event: ev.event });
      }
    }

    expect(observed).toEqual([
      { event: "session_ready" },
      { event: "message_delta", preview: "Hel" },
      { event: "message_delta", preview: "lo" },
      { event: "message_delta", preview: " " },
      { event: "message_delta", preview: "world" },
      { event: "message_complete", preview: "Hello world" },
      { event: "result" },
      { event: "done" },
    ]);
  });

  it("typed delta payload exposes text and message_id", async () => {
    const wire = streamingTextWire("m_typed", ["A", "B"]);
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch(wire),
    });
    const session = await client.createSession();
    const collected: DeliveredEvent[] = [];
    for await (const ev of session.events()) collected.push(ev);
    const deltas = collected.filter((e) => e.event === "message_delta");
    expect(deltas).toHaveLength(2);
    for (const d of deltas) {
      const data = d.data as { message_id: string; delta: { text?: string } };
      expect(data.message_id).toBe("m_typed");
      expect(typeof data.delta.text).toBe("string");
    }
  });

  it("session.lastEventId advances to the last delivered delta id", async () => {
    const wire = streamingTextWire("m_id", ["x", "y", "z"]);
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch(wire),
    });
    const session = await client.createSession();
    // tokens=3 → session_ready(1) + 3 deltas + complete + result + done = id 7
    let last: string | undefined;
    for await (const ev of session.events()) last = String(ev.id);
    expect(session.lastEventId).toBe(last);
    expect(session.lastEventId).toBe("7");
  });

  it("forwards input_json_delta deltas verbatim (for GenUI buffering)", async () => {
    const wire = [
      'id: 1\nevent: session_ready\ndata: {"session_id":"sess-1","protocol_version":"1.0"}\n\n',
      `id: 2\nevent: message_delta\ndata: ${JSON.stringify({
        message_id: "m_gen",
        delta: {
          type: "input_json_delta",
          partial_json: '{"location":',
          tool_use_id: "tu_42",
          name: "mcp__genui__render_weather_card",
        },
      })}\n\n`,
      `id: 3\nevent: message_delta\ndata: ${JSON.stringify({
        message_id: "m_gen",
        delta: {
          type: "input_json_delta",
          partial_json: '"Boston"}',
          tool_use_id: "tu_42",
        },
      })}\n\n`,
      'id: 4\nevent: done\ndata: {}\n\n',
    ].join("");
    const client = createAgentClient({
      baseUrl: "http://x",
      fetchImpl: makeFakeFetch(wire),
    });
    const session = await client.createSession();
    const deltas: Array<Record<string, unknown>> = [];
    for await (const ev of session.events()) {
      if (ev.event === "message_delta") {
        deltas.push((ev.data as { delta: Record<string, unknown> }).delta);
      }
    }
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
    const wire =
      'id: 1\nevent: message_delta\ndata: {"message_id":"m","delta":{"text":"A"}}\n\n' +
      ": keepalive\n\n" +
      'id: 2\nevent: message_delta\ndata: {"message_id":"m","delta":{"text":"B"}}\n\n';
    const out = feedSSE(s, wire);
    expect(out).toHaveLength(2);
    expect(out.map((e) => e.id)).toEqual(["1", "2"]);
  });
});
