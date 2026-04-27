# Changesets

This directory holds [changesets](https://github.com/changesets/changesets) used to drive the release workflow for `@agent-webkit/core` and `@agent-webkit/react`.

To add a changeset:

```sh
pnpm changeset
```

Pick the affected packages and a bump type. Commit the generated `.md` file alongside your code change. On merge to `main`, the release workflow opens (or updates) a "Version Packages" PR; merging that PR publishes to npm.

Note: `@agent-webkit/chat-demo` is ignored — it's an example, not a published package. The Python package `agent-webkit-server` is released independently via tag-triggered PyPI publish.
