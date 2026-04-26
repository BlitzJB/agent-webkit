# Recording harness

Drives the **real** Claude Agent SDK through scripted scenarios and dumps every yielded
message + every callback invocation to JSONL fixtures in `../../fixtures/`.

These fixtures are the ground truth for the mock SDK in `tests/fake_claude_sdk/`. Re-record
them on SDK version bumps.

## Running

Requires an Anthropic API key:

```sh
export ANTHROPIC_API_KEY=...
python record.py --scenario all --output ../../fixtures/
# Or a specific scenario:
python record.py --scenario plain_qa
```

## Scenarios

1. `plain_qa` — Plain text Q&A (baseline message stream shape)
2. `read_allow` — Single tool use: Read with permission allowed
3. `read_deny` — Permission denied with `interrupt=True`
4. `permission_rewrite` — Permission with `updated_input` rewrite
5. `mid_stream_interrupt` — Mid-stream `client.interrupt()` (capture buffer-drain)
6. `hook_pretool_block` — Hook firing — PreToolUse block decision
7. `multi_turn_queued` — Multi-turn with queued messages
8. `image_attachment` — Image attachment turn (base64)
9. `ask_user_question` — AskUserQuestion turn (capture exact input/output shape)
10. `mcp_tool_call` — MCP server connect + tool call
11. `resume_session` — Resume session by ID

Each scenario writes `<scenario>.jsonl` in the output dir.
