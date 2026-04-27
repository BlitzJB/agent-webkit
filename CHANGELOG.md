# Changelog

This is the aggregate changelog. Per-package changelogs live under each package directory.

## v0.2.0 — 2026-04-28

First public release across all three packages.

- `@agent-webkit/core@0.2.0` — npm.
- `@agent-webkit/react@0.2.0` — npm.
- `agent-webkit-server@0.2.0` — PyPI.

Highlights:

- Wire protocol pinned at `1.0`. Full event/message catalog: [Wire protocol reference](https://agent-webkit-docs.vercel.app/docs/reference/wire-protocol).
- Multi-subscriber SSE fan-out with `Last-Event-ID` resume.
- Permission RPC and `AskUserQuestion` routing as first-class wire events.
- FastAPI adapter and Postgres session adapter bundled.
- React hook with delta reconciliation, permission UI states, and reconnect.
- Documentation site live at [agent-webkit-docs.vercel.app](https://agent-webkit-docs.vercel.app).
