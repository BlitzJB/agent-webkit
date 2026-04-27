# agent-webkit

Drive [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk/python) sessions from the web — with streaming, permission approvals, `AskUserQuestion`, and reconnect handled for you.

**Docs: [agent-webkit-docs.vercel.app](https://agent-webkit-docs.vercel.app)**

## What's in here

| Package                                                 | Registry                                                 | What it is                                                |
| ------------------------------------------------------- | -------------------------------------------------------- | --------------------------------------------------------- |
| [`@agent-webkit/core`](./packages/core)                 | [npm](https://www.npmjs.com/package/@agent-webkit/core)  | Isomorphic JS transport (browser, Node, Deno, Bun).       |
| [`@agent-webkit/react`](./packages/react)               | [npm](https://www.npmjs.com/package/@agent-webkit/react) | `useAgentSession()` React hook.                           |
| [`agent-webkit-server`](./packages/agent-webkit-server) | [PyPI](https://pypi.org/project/agent-webkit-server/)    | Python server: session lifecycle, FastAPI + Postgres adapters. |

## Quick start

```bash
pip install "agent-webkit-server[fastapi]"
npm install @agent-webkit/react @agent-webkit/core
```

Server (`main.py`):

```python
from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig

app = create_app(auth=AuthConfig.from_env())
```

Client (`App.tsx`):

```tsx
import { useAgentSession } from "@agent-webkit/react";

const session = useAgentSession({ baseUrl: "http://127.0.0.1:8000" });
session.send("Hello");
```

Full walkthrough: **[docs/getting-started](https://agent-webkit-docs.vercel.app/docs/getting-started)**.

## Status

`v0.2.0` — public alpha. Wire protocol pinned at `1.0`. Pre-1.0 packages may break Python/JS APIs between minors; the wire stays stable.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Bugs and feature requests: [issues](https://github.com/BlitzJB/agent-webkit/issues).

## License

MIT — see [LICENSE](./LICENSE).
