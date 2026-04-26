import React, { useState } from "react";
import { useAgentSession } from "@agent-webkit/react";

export function App() {
  const baseUrl = (import.meta as any).env?.VITE_AGENT_URL ?? "/";
  const token = (import.meta as any).env?.VITE_AGENT_TOKEN as string | undefined;

  const session = useAgentSession({
    baseUrl,
    ...(token !== undefined ? { token } : {}),
  });

  const [draft, setDraft] = useState("");

  const onSend = async () => {
    if (!draft.trim()) return;
    const text = draft;
    setDraft("");
    await session.send(text);
  };

  return (
    <div style={{ maxWidth: 720, margin: "0 auto", padding: 16, fontFamily: "system-ui, sans-serif" }}>
      <h1 style={{ fontSize: 18 }}>agent-webkit chat demo</h1>
      <div style={{ fontSize: 12, color: "#666" }}>
        status: {session.status} {session.sessionId ? `· session ${session.sessionId.slice(0, 8)}` : ""}
        {session.totalCostUsd > 0 ? ` · $${session.totalCostUsd.toFixed(4)}` : ""}
      </div>

      <div style={{ border: "1px solid #ddd", borderRadius: 8, padding: 12, minHeight: 320, marginTop: 12 }}>
        {session.messages.map((m) => (
          <MessageView key={m.id} m={m} />
        ))}
        {session.lastError && (
          <div style={{ color: "#c00", marginTop: 8 }}>
            error: {session.lastError.code} — {session.lastError.message}
          </div>
        )}
      </div>

      {session.pendingPermission && (
        <PermissionPrompt
          req={session.pendingPermission}
          onAllow={() => session.approve(session.pendingPermission!.correlation_id)}
          onDeny={() => session.deny(session.pendingPermission!.correlation_id, { interrupt: true })}
        />
      )}

      {session.pendingQuestion && (
        <QuestionPrompt
          q={session.pendingQuestion}
          onAnswer={(answers) => session.answer(session.pendingQuestion!.correlation_id, answers)}
        />
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onSend()}
          placeholder="Type a message…"
          style={{ flex: 1, padding: 8, fontSize: 14 }}
          disabled={session.status === "awaiting_permission" || session.status === "awaiting_question"}
        />
        <button onClick={onSend}>Send</button>
        <button onClick={() => session.interrupt()} disabled={session.status === "idle"}>
          Interrupt
        </button>
      </div>
    </div>
  );
}

function MessageView({ m }: { m: ReturnType<typeof useAgentSession>["messages"][number] }) {
  if (m.kind === "user") {
    return (
      <div style={{ marginBottom: 8 }}>
        <strong>you:</strong> {typeof m.content === "string" ? m.content : "(content blocks)"}
      </div>
    );
  }
  if (m.kind === "assistant") {
    return (
      <div style={{ marginBottom: 8 }}>
        <strong>assistant:</strong>{" "}
        {m.content.map((b, i) => {
          if (b.type === "text") return <span key={i}>{b.text}</span>;
          if (b.type === "tool_use")
            return (
              <code key={i} style={{ background: "#eef", padding: "0 4px" }}>
                {b.name}({JSON.stringify(b.input)})
              </code>
            );
          return null;
        })}
        {m.streaming && <span style={{ color: "#aaa" }}> ▍</span>}
      </div>
    );
  }
  return (
    <div style={{ marginBottom: 8, fontSize: 12, color: m.is_error ? "#c00" : "#080" }}>
      ↳ tool result: {String(m.output).slice(0, 200)}
    </div>
  );
}

function PermissionPrompt({
  req,
  onAllow,
  onDeny,
}: {
  req: { tool_name: string; input: unknown };
  onAllow: () => void;
  onDeny: () => void;
}) {
  return (
    <div style={{ border: "1px solid #fc0", padding: 12, borderRadius: 8, marginTop: 12, background: "#fffbe6" }}>
      <div>
        Allow <code>{req.tool_name}</code>?
      </div>
      <pre style={{ fontSize: 11, background: "#fff", padding: 6, overflow: "auto" }}>
        {JSON.stringify(req.input, null, 2)}
      </pre>
      <button onClick={onAllow}>Allow</button>
      <button onClick={onDeny} style={{ marginLeft: 8 }}>
        Deny + interrupt
      </button>
    </div>
  );
}

function QuestionPrompt({
  q,
  onAnswer,
}: {
  q: { questions: { questions: { question: string; options: { label: string }[] }[] } };
  onAnswer: (answers: unknown) => void;
}) {
  const items = q.questions.questions ?? [];
  const [selected, setSelected] = useState<Record<number, string[]>>({});

  return (
    <div style={{ border: "1px solid #06c", padding: 12, borderRadius: 8, marginTop: 12, background: "#eef6ff" }}>
      {items.map((item, idx) => (
        <div key={idx} style={{ marginBottom: 8 }}>
          <div>{item.question}</div>
          <div>
            {item.options.map((opt) => {
              const isSel = (selected[idx] ?? []).includes(opt.label);
              return (
                <button
                  key={opt.label}
                  onClick={() =>
                    setSelected((prev) => {
                      const cur = prev[idx] ?? [];
                      return { ...prev, [idx]: cur.includes(opt.label) ? cur.filter((x) => x !== opt.label) : [...cur, opt.label] };
                    })
                  }
                  style={{ marginRight: 4, background: isSel ? "#cdf" : undefined }}
                >
                  {opt.label}
                </button>
              );
            })}
          </div>
        </div>
      ))}
      <button
        onClick={() =>
          onAnswer(
            items.map((item, idx) => ({ question: item.question, selectedOptions: selected[idx] ?? [] }))
          )
        }
      >
        Submit
      </button>
    </div>
  );
}
