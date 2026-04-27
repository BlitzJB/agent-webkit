"""Reference deployment of agent-webkit-server's FastAPI adapter.

Mirrors what a typical consumer needs: build the app, configure auth from
the environment, run uvicorn. The interesting code lives in the library at
``agent_webkit_server.adapters.fastapi``; this script is intentionally thin.
"""
from __future__ import annotations

import argparse
import os

import uvicorn

from agent_webkit_server.adapters.fastapi import create_app
from agent_webkit_server.auth import AuthConfig


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-auth", action="store_true", help="Disable bearer-token auth (dev only)")
    p.add_argument("--token", default=os.environ.get("AGENT_WEBKIT_TOKEN"))
    args = p.parse_args()

    auth = AuthConfig(disabled=args.no_auth, token=args.token)
    app = create_app(auth=auth)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
