"""Bearer token auth — togglable via env or constructor flag."""
from __future__ import annotations

import os
from typing import Optional

from fastapi import HTTPException, Request


class AuthConfig:
    def __init__(self, *, token: Optional[str] = None, disabled: bool = False) -> None:
        self.token = token
        self.disabled = disabled

    @classmethod
    def from_env(cls) -> "AuthConfig":
        if os.environ.get("AGENT_WEBKIT_NO_AUTH") == "1":
            return cls(disabled=True)
        return cls(token=os.environ.get("AGENT_WEBKIT_TOKEN"))


def require_auth(config: AuthConfig):
    """FastAPI dependency factory."""
    async def dep(request: Request) -> None:
        if config.disabled:
            return
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="Missing bearer token")
        token = header[len("Bearer "):].strip()
        if not config.token or token != config.token:
            raise HTTPException(status_code=401, detail="Invalid token")
    return dep
