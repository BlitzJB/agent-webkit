"""Optional adapters that plug into the Claude Agent SDK but are not required
to run the agent-webkit reference server.

Each module in this package depends on extra third-party libraries (e.g.
``asyncpg`` for ``pg_session_store``). They are imported lazily so the core
server has no hard dependency on them.
"""
