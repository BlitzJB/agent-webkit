# chat-demo

End-to-end Vite + React demo using `@agent-webkit/react`.

```sh
# 1. Start the reference server
cd ../../server-reference
pip install -e .
python -m server.main --no-auth

# 2. Start the Vite dev server
cd ../examples/chat-demo
pnpm dev
```

Then open http://localhost:5173. The dev server proxies `/sessions` to `http://localhost:8000`.

To run with auth, set `VITE_AGENT_TOKEN=...` and start the server with `AGENT_WEBKIT_TOKEN=...`.
