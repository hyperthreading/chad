# chad as an ACP agent (`chad acp`)

`chad acp` runs chad as a long-lived subprocess speaking Agent Client
Protocol v1 over stdio (JSON-RPC 2.0, one object per line), so editors like
Zed can drive it. Protocol traffic uses stdout only; logs and model-load
progress go to stderr.

## Zed setup

Register chad as a custom agent server in Zed `settings.json`:

```json
{
  "agent_servers": {
    "chad": {
      "type": "custom",
      "command": "chad",
      "args": ["acp"],
      "env": {}
    }
  }
}
```

The `chad` command must resolve inside Zed (the `chad-code` PyPI
install provides it). From a dev clone, point `command` at `uv` with
`args` `["run", "--project", "/path/to/chad", "chad", "acp"]` instead.
The first prompt downloads the model once (~13 GB) into the shared
Hugging Face cache, exactly like the first `chad` run.

Flags: `--model`, `--yolo` and `--plan` set the starting session mode
(normal by default; switchable per session), `--no-think` matches the
main CLI. `CHAD_*` environment knobs work unchanged.

## Protocol coverage (v1)

Handled: `initialize`, `authenticate` (no-op, single-user local agent),
`session/new`, `session/prompt`, `session/cancel`, `$/cancel_request`
and `session/set_mode` (modes normal, auto, yolo, plan).
Unimplemented methods answer method-not-found; ACP sessions live for the
process lifetime. Every turn still persists to chad's own session store,
so `chad -c` can pick the thread up afterwards.

Mapping: `run_turn` streams text as `agent_message_chunk` and reasoning
as `agent_thought_chunk`; dispatched tools announce `tool_call` and close
with `tool_call_update` (edits carry a `diff` payload); `_confirm`
becomes `session/request_permission` with allow-once, allow-always
(remembered for the session) and reject-once. `stopReason` is
`cancelled` on cancel, `max_turn_requests` when the governor or step cap
banked a progress note, else `end_turn`.

## Honest limits

- One engine turn runs at a time (the MLX KV cache is a single live
  object): a second prompt while one runs answers `busy`.
- Prompts chdir into their session cwd under that same lock.
- Concurrent sessions share the module-global MCP and todo state; one
  active session at a time is the supported setup.
- Client-sent `mcpServers` connect like user-level config (trusted, no
  project gate): stdio and streamable HTTP only, SSE entries are skipped
  with a note. The project's own `.mcp.json` still loads underneath, and
  a client entry wins a same-name conflict. The first prompt of a session
  reports one `MCP:` info line with server tool counts, errors and skips.
- Images and audio become placeholders (text-only model); embedded text
  resources are inlined into the prompt.

## Tests

- `uv run pytest tests/test_acp.py -q`: framing, permission allow and
  reject, allow-always memory, cancel mid-prompt and mid-permission,
  busy, bad params, set_mode, content-block flattening.
- `tests/acp_client` (`npm install && npm test`): the same ground driven
  through the real stdio transport by the official TypeScript client
  (`@zed-industries/agent-client-protocol`, pinned in package.json),
  whose zod schemas validate every message chad sends.
- `tests/acp_client/live.mjs` (`node live.mjs`): one tiny real-weights task
  through the same official client. Manual only: it downloads nothing new
  but needs the cached model, auto-approves permissions, and takes minutes.
- `tests/test_mcp.py` client-overlay tests plus `test_client_mcp_end_to_end`
  in `tests/test_acp.py`: a real stub MCP server over stdio, driven through
  a real `Agent` and the ACP transport with no weights.
