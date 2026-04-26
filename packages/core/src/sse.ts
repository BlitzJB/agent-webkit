// Minimal SSE parser used by the fetch-based transport.
//
// Why we have our own: the browser-native `EventSource` cannot send custom headers
// (so it's unusable when auth is enabled), and we don't want to drag in a polyfill
// dependency at L1. Node 18+ exposes the WHATWG fetch+ReadableStream we need.

export interface ParsedSSEEvent {
  id?: string;
  event?: string;
  data: string;
}

/**
 * Parse a chunk of SSE text into events. Holds incomplete trailing data in `state`.
 * SSE framing per https://html.spec.whatwg.org/multipage/server-sent-events.html.
 */
export interface SSEParserState {
  buffer: string;
  current: { id?: string; event?: string; data: string[] };
}

export function newSSEParserState(): SSEParserState {
  return { buffer: "", current: { data: [] } };
}

export function feedSSE(state: SSEParserState, chunk: string): ParsedSSEEvent[] {
  state.buffer += chunk;
  const out: ParsedSSEEvent[] = [];

  while (true) {
    const nl = state.buffer.indexOf("\n");
    if (nl === -1) break;
    let line = state.buffer.slice(0, nl);
    state.buffer = state.buffer.slice(nl + 1);
    if (line.endsWith("\r")) line = line.slice(0, -1);

    if (line === "") {
      // Dispatch
      if (state.current.data.length > 0 || state.current.event || state.current.id) {
        const ev: ParsedSSEEvent = { data: state.current.data.join("\n") };
        if (state.current.event !== undefined) ev.event = state.current.event;
        if (state.current.id !== undefined) ev.id = state.current.id;
        out.push(ev);
      }
      state.current = { data: [] };
      continue;
    }

    if (line.startsWith(":")) continue; // comment/keepalive

    const colon = line.indexOf(":");
    let field: string;
    let value: string;
    if (colon === -1) {
      field = line;
      value = "";
    } else {
      field = line.slice(0, colon);
      value = line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
    }

    switch (field) {
      case "event":
        state.current.event = value;
        break;
      case "data":
        state.current.data.push(value);
        break;
      case "id":
        state.current.id = value;
        break;
      // ignore retry, unknown
    }
  }

  return out;
}
