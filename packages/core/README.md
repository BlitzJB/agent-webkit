# @agent-webkit/core

L1 vanilla JS SDK for the agent-webkit wire protocol. Isomorphic — runs in browsers,
Node 18+, Deno, and Bun. No React, no UI, no transitive deps.

## Install

```sh
pnpm add @agent-webkit/core
```

## Usage

```ts
import { createAgentClient } from "@agent-webkit/core";

const client = createAgentClient({ baseUrl: "http://localhost:8000", token: "..." });
const session = await client.createSession({ model: "claude-opus-4-7" });

await session.send("Read README.md and summarize it.");

for await (const ev of session.events()) {
  switch (ev.event) {
    case "message_delta":
      // stream tokens
      break;
    case "permission_request":
      await session.approve(ev.data.correlation_id);
      break;
    case "ask_user_question":
      await session.answer(ev.data.correlation_id, { /* per AskUserQuestion shape */ });
      break;
    case "done":
      return;
  }
}
```

## Transport selection

If `token` is provided, uses fetch-based SSE (works in Node + browsers, supports custom headers).
If no token, native `EventSource` could be used, but the fetch-based path is used uniformly to
keep behavior consistent across runtimes. Both approaches honor `Last-Event-ID` for resume.

## Resume

```ts
const session = client.attachSession(savedId, { resumeFromEventId: savedSeq });
for await (const ev of session.events()) { /* ... */ }
```

If the requested seq has been evicted from the server's ring buffer, `events()` throws a
`TransportError` with status 412.
