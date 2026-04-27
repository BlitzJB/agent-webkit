# Contributing to agent-webkit

Thanks for considering a contribution. This is a small project; we move fast and we read every PR.

## Layout

- `packages/core` — `@agent-webkit/core`, isomorphic JS transport.
- `packages/react` — `@agent-webkit/react`, React hook.
- `packages/agent-webkit-server` — Python server library.
- `docs/` — Fumadocs site, deployed to Vercel.
- `examples/` — sample apps (`chat-demo` etc).

## Dev setup

```bash
git clone https://github.com/BlitzJB/agent-webkit
cd agent-webkit
pnpm install                    # JS workspace
cd packages/agent-webkit-server && pip install -e ".[fastapi,postgres]"
```

JS:

```bash
pnpm -r --filter "./packages/*" build
pnpm -r --filter "./packages/*" test
```

Python:

```bash
cd packages/agent-webkit-server
pytest
```

Docs:

```bash
cd docs
pnpm dev
```

## Pull requests

- Open an issue first for anything non-trivial. We'd rather discuss the shape before you sink time into a PR.
- One logical change per PR. Refactors and feature work go in separate PRs.
- For JS packages, run a `pnpm changeset` before pushing — versioning is automated by changesets.
- For the Python package, the version in `pyproject.toml` is the source of truth. We'll bump it on merge.
- The wire protocol is pinned at `1.0`. Wire-breaking changes need a major bump *and* a PR-level discussion.

## Tests

- Don't mock the SDK. We use a real `ClaudeSDKClient` in test fixtures via a controlled harness — see `packages/agent-webkit-server/tests/`.
- For the JS side, `cross-runtime.test.ts` exercises the transport against a live Python server. Slow but the only way to catch wire-level regressions.

## Code style

- TypeScript: project is strict-mode. No `any` without a comment explaining why.
- Python: type hints on every public surface. We don't run mypy in CI yet but PRs that add hints are welcome.

## Releasing

Maintainers only.

- JS: merge a changesets PR; the `release-js.yml` workflow publishes to npm.
- Python: tag `py-vX.Y.Z`; the `release-pypi.yml` workflow publishes via OIDC trusted publishing.

## Reporting issues

[github.com/BlitzJB/agent-webkit/issues](https://github.com/BlitzJB/agent-webkit/issues).

For bugs, please include:

- Versions of all three packages you're using.
- The wire event sequence, copied from the network panel.
- A minimal reproducer if the bug is in client code.
