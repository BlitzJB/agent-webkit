# @agent-webkit/react

L2 React abstractions over `@agent-webkit/core`.

```tsx
import { useAgentSession } from "@agent-webkit/react";

export function Chat() {
  const {
    messages, status, pendingPermission, pendingQuestion,
    send, interrupt, approve, deny, answer,
  } = useAgentSession({ baseUrl: "http://localhost:8000", token: "..." });

  return (
    <div>
      {messages.map((m) => <Bubble key={m.id} m={m} />)}
      {pendingPermission && (
        <PermissionPrompt
          req={pendingPermission}
          onAllow={() => approve(pendingPermission.correlation_id)}
          onDeny={() => deny(pendingPermission.correlation_id)}
        />
      )}
      {pendingQuestion && (
        <QuestionPrompt
          q={pendingQuestion}
          onAnswer={(answers) => answer(pendingQuestion.correlation_id, answers)}
        />
      )}
    </div>
  );
}
```

The hook handles delta→complete reconciliation by `message_id`, race-resolution UI states,
and reconnect with `Last-Event-ID`.
