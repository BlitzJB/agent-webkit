import React, { useState } from "react";
import { useAgentSession, useGenerativeUI } from "@agent-webkit/react";

export function App() {
  const baseUrl = (import.meta as any).env?.VITE_AGENT_URL ?? "/";
  const token = (import.meta as any).env?.VITE_AGENT_TOKEN as string | undefined;

  const ui = useGenerativeUI({
    schemaUrl: `${baseUrl.replace(/\/$/, "")}/genui/schema`,
    renderers: {
      weather_card: (props) => <WeatherCard {...(props as WeatherProps)} />,
      pricing_table: (props) => <PricingTable {...(props as PricingProps)} />,
    },
  });

  const session = useAgentSession({
    baseUrl,
    ...(token !== undefined ? { token } : {}),
    onEvent: ui.onEvent,
  });

  const [draft, setDraft] = useState("");

  const onSend = async () => {
    const t = draft.trim();
    if (!t) return;
    setDraft("");
    await session.send(t);
  };

  return (
    <div style={{ maxWidth: 720, margin: "0 auto", padding: 16, fontFamily: "system-ui, sans-serif" }}>
      <h1 style={{ fontSize: 18 }}>generative UI demo</h1>
      <div style={{ fontSize: 12, color: "#666" }}>
        status: {session.status} · {ui.schema?.tools.length ?? 0} renderers loaded
      </div>

      <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 12 }}>
        {ui.updates.map((u) => (
          <div key={u.toolUseId} style={{ opacity: u.complete ? 1 : 0.7 }}>
            {ui.render(u)}
          </div>
        ))}
        {ui.updates.length === 0 && (
          <div style={{ color: "#999" }}>
            Try: <em>"Show me the weather in Boston"</em> or <em>"Compare your pricing plans"</em>
          </div>
        )}
      </div>

      <div style={{ display: "flex", gap: 8, marginTop: 16 }}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onSend()}
          placeholder="Ask the agent to render something…"
          style={{ flex: 1, padding: 8, fontSize: 14 }}
        />
        <button onClick={onSend}>Send</button>
      </div>
    </div>
  );
}

interface WeatherProps {
  location: string;
  temperature_f?: number;
  condition?: string;
}

function WeatherCard(p: WeatherProps) {
  return (
    <div style={{ border: "1px solid #ace", borderRadius: 12, padding: 16, background: "#f0f8ff" }}>
      <div style={{ fontSize: 12, color: "#456" }}>weather</div>
      <div style={{ fontSize: 22, fontWeight: 600 }}>{p.location}</div>
      {p.temperature_f !== undefined && (
        <div style={{ fontSize: 36 }}>{Math.round(p.temperature_f)}°F</div>
      )}
      {p.condition && <div style={{ color: "#467" }}>{p.condition}</div>}
    </div>
  );
}

interface PricingProps {
  plans?: Array<{ name: string; price: number; features?: string[] }>;
}

function PricingTable(p: PricingProps) {
  const plans = p.plans ?? [];
  return (
    <div style={{ display: "grid", gridTemplateColumns: `repeat(${Math.max(1, plans.length)}, 1fr)`, gap: 12 }}>
      {plans.map((plan, i) => (
        <div key={i} style={{ border: "1px solid #ddd", borderRadius: 12, padding: 16 }}>
          <div style={{ fontWeight: 600 }}>{plan.name}</div>
          <div style={{ fontSize: 24, marginTop: 4 }}>${plan.price}<span style={{ fontSize: 12, color: "#888" }}>/mo</span></div>
          {plan.features && (
            <ul style={{ marginTop: 8, paddingLeft: 18, fontSize: 13 }}>
              {plan.features.map((f, j) => <li key={j}>{f}</li>)}
            </ul>
          )}
        </div>
      ))}
    </div>
  );
}
