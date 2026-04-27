# server-fastapi (example)

Thin reference deployment of `agent-webkit-server`'s FastAPI adapter.

```bash
pip install "agent-webkit-server[fastapi]"
python main.py --no-auth --port 8000          # dev
AGENT_WEBKIT_TOKEN=secret python main.py      # with auth
```

The 30-line `main.py` is the entire integration — everything else lives in the library.
