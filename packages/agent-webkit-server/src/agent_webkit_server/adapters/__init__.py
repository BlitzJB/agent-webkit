"""Optional adapters bundled with agent-webkit-server.

These are import-on-demand. None of them are imported by the core, so heavy
deps (asyncpg, fastapi, uvicorn) only load when a consumer asks for them.
"""
