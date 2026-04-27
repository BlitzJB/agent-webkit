# Security policy

## Supported versions

`v0.2.x` is the only supported line. Older 0.1.x is unsupported — please upgrade.

## Reporting a vulnerability

**Do not open a public issue** for security reports.

Email **blitzjb@protonmail.com** with:

- A description of the issue and its impact.
- Steps to reproduce.
- The version(s) affected.
- Your name and any preferred attribution (or "anonymous").

We aim to:

- Acknowledge within 72 hours.
- Confirm or dispute within 7 days.
- Ship a fix within 30 days for confirmed issues, faster for severe ones.

## Scope

In scope:

- Wire protocol bugs that allow auth bypass, session hijack, or cross-session leakage.
- Adapter bugs in `agent-webkit-server` (FastAPI, Postgres) with a security impact.
- Client-side bugs that leak session tokens, conversation content, or permission decisions.

Out of scope:

- Issues in the underlying [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) — please report those upstream.
- Denial of service via legitimate API use (e.g. opening many sessions); operate behind a rate limiter.
- Issues that require a malicious server (we trust the server side; a malicious server can do anything to its clients).

## Disclosure

Coordinated disclosure preferred. We'll credit reporters in the release notes unless you opt out.
