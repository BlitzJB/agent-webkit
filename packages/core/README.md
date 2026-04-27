# @agent-webkit/core

Typed, isomorphic JS transport for the [agent-webkit](https://agent-webkit-docs.vercel.app) wire protocol.

```bash
npm install @agent-webkit/core
```

Runs in browsers, Node ≥ 18, Deno, Bun, and Cloudflare Workers.

## Use

```ts
import { createAgentClient } from "@agent-webkit/core";

const client = createAgentClient({ baseUrl: "https://api.example.com", token });
const session = await client.createSession();

await session.send("Hello");

for await (const event of session.events()) {
  if (event.event === "permission_request") {
    await session.approve(event.data.correlation_id);
  }
}
```

- **Streaming** with `for await`.
- **Auto-reconnect** via `Last-Event-ID`.
- **Permission / question RPC** as first-class methods.
- Full type definitions for every wire event.

## Docs

- [Vanilla JS guide](https://agent-webkit-docs.vercel.app/docs/guides/frontend-vanilla)
- [Core API reference](https://agent-webkit-docs.vercel.app/docs/reference/core-api)
- [Wire protocol](https://agent-webkit-docs.vercel.app/docs/reference/wire-protocol)

## License

MIT
