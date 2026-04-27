# @agent-webkit/react

React hook for the [agent-webkit](https://agent-webkit-docs.vercel.app) wire protocol. Streams Claude Agent SDK sessions into typed reactive state.

```bash
npm install @agent-webkit/react @agent-webkit/core
```

## Use

```tsx
import { useAgentSession } from "@agent-webkit/react";

function Chat() {
  const session = useAgentSession({ baseUrl: "https://api.example.com", token });

  return (
    <>
      <Messages list={session.messages} />
      <Composer
        disabled={session.status !== "idle"}
        onSend={session.send}
      />
      {session.pendingPermission && (
        <Modal>
          <button onClick={() => session.approve(session.pendingPermission!.correlation_id)}>
            Allow
          </button>
        </Modal>
      )}
    </>
  );
}
```

What you get for free:

- **Delta reconciliation** into a typed `messages` list.
- **Permission UI states** via `pendingPermission` + `approve` / `deny`.
- **`AskUserQuestion`** routing via `pendingQuestion` + `answer`.
- **Reconnect** transparently after Wi-Fi blips.
- Multi-tab race semantics handled.

## Docs

- [React guide](https://agent-webkit-docs.vercel.app/docs/guides/frontend-react)
- [React API reference](https://agent-webkit-docs.vercel.app/docs/reference/react-api)
- [Permissions guide](https://agent-webkit-docs.vercel.app/docs/guides/permissions)

## License

MIT
