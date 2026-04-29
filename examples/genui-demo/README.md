# Generative UI demo

End-to-end example of agent-webkit's generative-UI feature: define typed
components on the server, let the agent invoke them as tools, render them as
React components on the client.

## What's here

- `server/main.py` — FastAPI server with two registered components
  (`WeatherCard`, `PricingTable`). Run on port 8000.
- `src/App.tsx` — React frontend. Uses `useGenerativeUI` to dispatch
  incoming `tool_use` events to renderer components.

## Running it

```bash
# 1. Server (needs a Claude API key or OAuth token in your env)
cd examples/genui-demo/server
python main.py --no-auth

# 2. Client
cd examples/genui-demo
pnpm install
pnpm dev    # http://localhost:5174
```

Then ask things like:
- "Show me the weather in Boston (it's 72°F and sunny)"
- "Compare your pricing plans: basic at $9, pro at $29 with priority support"

The agent will call the matching render tool; the frontend will receive the
`tool_use` event, look up the renderer by `short_name`, and mount the
component with the validated props.
