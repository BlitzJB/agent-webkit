import { describe, it, expect } from "vitest";
import { feedSSE, newSSEParserState } from "../src/sse.js";

describe("SSE parser", () => {
  it("parses a single event", () => {
    const s = newSSEParserState();
    const out = feedSSE(s, "event: hi\ndata: {\"x\":1}\nid: 7\n\n");
    expect(out).toEqual([{ event: "hi", data: '{"x":1}', id: "7" }]);
  });

  it("joins multiline data with newlines", () => {
    const s = newSSEParserState();
    const out = feedSSE(s, "data: a\ndata: b\n\n");
    expect(out).toEqual([{ data: "a\nb" }]);
  });

  it("ignores comments (keepalives)", () => {
    const s = newSSEParserState();
    const out = feedSSE(s, ":keepalive\n\n");
    expect(out).toEqual([]);
  });

  it("buffers across chunks", () => {
    const s = newSSEParserState();
    const a = feedSSE(s, "event: x\ndata: hel");
    const b = feedSSE(s, "lo\n\n");
    expect(a).toEqual([]);
    expect(b).toEqual([{ event: "x", data: "hello" }]);
  });

  it("handles CRLF line endings", () => {
    const s = newSSEParserState();
    const out = feedSSE(s, "event: e\r\ndata: d\r\n\r\n");
    expect(out).toEqual([{ event: "e", data: "d" }]);
  });
});
