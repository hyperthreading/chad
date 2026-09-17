# Chad ACP Bridge — PLAN (rev 2, after two independent reviews)

Thin local piece for fast-remote setups. The full Chad (harness plus MLX
engine, in-process) runs on a remote Apple Silicon Mac. Locally, where Zed
and the files are, a bridge process does exactly two jobs: prompt delivery
with response rendering, and tool execution. There is no agent loop locally,
so this document never calls it a harness.

Status: IMPLEMENTED and live-verified (ssh loopback, real 27B weights,
official TypeScript client: enumerate, read, multi-permission bash/write,
cancel). Rev 2 folded in two external reviews (default-model and GLM
reviewers). The live runs found four bugs the model-free gate cannot see,
all fixed: the remote scratch went over as `~`-prefixed (the remote
session/new checks the literal path, so provision now resolves the remote
`$HOME` and hands back an absolute dir); the ssh forward aimed at a
probed-then-closed port while the hub bound a different one (the hub now
binds first and the forward aims at its real port); the permission answer
crossed unenveloped (bare outcome object, parsed as unknown and denied;
the relay harness now asserts the granted verdict, not just the
round trip); and every ACP prompt shared a fresh worker thread while the
MLX engine binds streams to its first thread (turn two crashed live, so
prompts now share one worker, pinned by a unit test).

## 0. Gate before code (from review)

Schemas, prompts, and guardrail-adjacent behavior are model-visible. The
repo requires an issue plus maintainer eval conversation before building
those (AGENTS.md). Open it first: bare-name schemas, the missing `read`
tool (a measured decision reversal, needs one explicit sentence), and the
`auto_approves` exclusion. This is the single biggest schedule risk and
sits off the critical path of Phase 0.

## 1. Architecture: 3 processes, 3 channels

- Zed (local, owns files) talks ACP over stdio to the bridge.
- The bridge talks ACP over ssh-stdio to remote `chad acp --no-builtins
  {server}`. One ssh invocation also carries the TCP forward.
- The bridge serves MCP over loopback HTTP. The forward direction is `-R`
  (remote listens on `rport`, tunneled back to the bridge): `-L` would
expose a remote service locally and nothing would ever listen on the
remote loopback. Default `rport` 18789 with a `--remote-port` flag;
collision fails loudly via `ExitOnForwardFailure=yes`.
- Remote `session/new` gets the MCP URL through the existing client-MCP
overlay, so no remote config file changes.

Granularity is per tool call everywhere: bulk work runs locally at full
speed, the wire carries prompts, tool requests with results, and chunks.

## 2. Tool mapping (exact, revised)

Bare names are kept in `active_schemas()` (schemas copied verbatim from
the MCP server) and `dispatch_for` re-targets them. Hiding names breaks
validation, which runs before dispatch, so hide-plus-deny is out.

| Model emits (remote) | Resolves to | Args | Notes |
|---|---|---|---|
| `bash` {command, timeout} | `mcp__local__bash` | identical copy | builtin hidden behind overlay |
| `write` {path, content} | `mcp__local__write` | identical copy | builtin hidden behind overlay |
| `edit` {path, old, new} | `mcp__local__edit` | identical copy | builtin hidden behind overlay |
| `read` {path, offset?, limit?} | `mcp__local__read` | new tool | cat -n style line numbers; 20k cap |
| `write_todos`, `done`, `finish`, `stop` | builtin, untouched | - | loop machinery |

Overlay ambiguity (two servers exposing one raw name) means no overlay
and a warning. `validate.py` needs no change: it reads the live schemas
it already reads. `alias_to_bash` output flows into the same overlay, so
read-aliases land on MCP instead of the denial the first draft caused.

Backing is Zed capability first, local execution fallback, negotiated at
`initialize` from `clientCapabilities`. Reads use `fs/read_text_file`
(bridge slices and caps; whole-file transfer acknowledged). Writes use
`fs/write_text_file` (native diff and buffer coherence come free, but
only for `write`; see section 7). `edit` has no Zed primitive: bridge
does read-modify-write with unique-match errors mirroring chad strings
pinned in tests. `bash` wraps the shell string in `/bin/sh -c` and
mediates `terminal/create`, `output` polling, `wait_for_exit`, `kill` on
timeout or cancel, `release` at close, shaped with `[exit N]`. v1 buffers
terminal output to exit (no partial streaming); bash and terminal output
both clip at the chad 20k convention, documented as double truncation
with the remote 4k finish cap.

## 3. Remote `--no-builtins {server}` seam (revised)

- `active_schemas`: hide `bash`, `write`, `edit`; present the named
  server raw-name copies instead.
- `dispatch_for`: consult the overlay map before `DISPATCH`. No denier
  needed on this path anymore; keep a backstop denial only for names
  with no overlay target.
- `auto_approves`: explicitly exclude overlay names in every mode. Bare
  `write` and `edit` sit in the auto-approve set, so without this line
  remote auto mode silently approves local writes. One condition, big
  comment. The yolo-to-auto clamp stays as a backstop behind it.
- `plan_mode_verdict`, the destructive-bash screen, and checkpoint
  snapshot sites keep working: they see the bare names the model
  emitted. The destructive screen firing remotely is desired (it forces
  a relayed prompt). Its path check against the remote scratch tree
  passes vacuously, which is why section 8 re-screens locally.
- MCP tool annotations: `readOnlyHint: true` on `read`, absent on the
  mutators, or remote plan mode and read-only flows break.

## 4. ACP relay rules (revised)

- Handshake 1:1. `session/new` modes and models passthrough verbatim,
except the bridge merges its own `local` MCP entry and drops Zed-supplied
`command`-type servers (forwarding those would execute local editor
config on the remote box; `url`-type entries pass through).
- Real id-translation table with lifetimes: remote integer request ids
  get fresh bridge-to-Zed ids with a map back; `sessionId` translated
  both directions; remote `toolCallId` values namespaced per remote
  session before forwarding (they repeat as `call-1` per session).
  Entries drop on prompt response, turn end, and session end.
- Prompt, cancel (both `session/cancel` and `$​/cancel_request` against
  the prompt id), and `stopReason` relayed. No mid-turn steering: acp
has no steering method, so Zed prompts queue between turns. Stated,
  not implied.
- Permission: relay `allow-once` only. No bridge allowlist, no
  allow-always relay (name-keyed standing approval for `rm -rf` is
  indefensible across ssh). Every action shows the Zed prompt.
- `set_mode` forwards with the yolo-to-auto clamp retained as backstop.
- Bridge serializes sessions (depth 1, busy passthrough, Zed prompt
  stays open on error, never hangs).

## 5. Remote workspace and paths (new section; was the hole)

- Per session, the bridge provisions `~/.chad/bridge/<sid>` on the
  remote with one ssh exec (`mkdir -p`) and hands that cwd to remote
  `session/new` (which requires an existing directory).
- It then pushes local `CLAUDE.md` and `AGENTS.md` when present
  (size-capped), so conventions survive although the remote tree is
  otherwise empty. The system-prompt workspace snapshot over the
  scratch tree is acknowledged degraded, in writing.
- The model addresses the LOCAL tree: MCP tool descriptions pin that
  paths resolve against the Zed session cwd, absolute or relative.
  The bridge resolves relative paths against the local cwd, never its
  own process cwd.
- Remote session persistence lands on the remote disk (documented
  retention); local transcript is Zed plus bridge logs.

## 6. ssh spawn (exact shape, revised)

Argv array only, never a shell string: `ssh`, `-R`,
`{rport}:127.0.0.1:{lport}`, `-o`, `ExitOnForwardFailure=yes`, `-o`,
`BatchMode=yes`, `-o`, `ServerAliveInterval=30`, `-q`, `-o`,
`LogLevel=ERROR`, `{user}@{host}`, `chad`, `acp`, `--no-builtins`,
`{server}`. No `-A` ever. Host keys pinned via known_hosts (no
-silent accept); first stdout line must parse as JSON or the bridge
fails fast (banner hygiene). sshd `DisableForwarding` or `AllowTcpForwarding=no` surfaces as a loud forward failure at startup, documented as preflight.
Remote env only for non-secrets via repeatable `--remote-env KEY=VAL`
with exec-array spawn; secrets travel `session/new` params (stdin pipe),
never argv or env. The MCP bearer (128-bit or more, per bridge
lifetime, constant-time compare, static `Authorization` header the chad
client already supports) is minted locally and injected through the
overlay URL headers for the same reason.

## 7. Security: bridge-side guards are must-fix before live

Remote name-keyed screens (`bash` destructive patterns,
`outside_workspace` edits) pass vacuously against the scratch tree, so
the bridge re-screens against the LOCAL tree before any execution, on
both backing paths: workspace-root containment (outside root hard
denies with a message; strict in v1), destructive-bash patterns
escalate to the Zed permission with the command shown. Local fallback
tools reuse `tools.tool_bash`, `tool_write`, `tool_edit` directly to
inherit gates, seatbelt mirror (`set_context(False, cwd)`), and env
guards for free. Zed-terminal backing is stated unsandboxed. Local
reads and writes default to the workspace root; `~/.ssh`, `~/.aws`,
`.env`, and browser profiles are denied by default list. Live tests
stay off until this section and the bearer land.

## 8. Observability and failure interplay

- Remote stderr forwards to bridge stderr with a `[remote]` prefix;
  NDJSON stdout carries ACP only. One correlation id per turn across
  all three processes in logs.
- Chaos tests (required, Phase 3): kill ssh mid-tool, deny
  mid-permission, cancel mid-terminal-wait, `rport` collision, forward
  refused by sshd, remote `service()` rebuild storms (fixed remote cwd
  per session means none expected; assert it).

## 9. Phases with acceptance gates (revised: 8-12 focused days)

- Phase -1 (off critical path): maintainer eval conversation for the
  model-visible surface (bare schemas, `read` reversal sentence,
  `auto_approves` exclusion). Nothing in Phase 1 closes without it.
- Phase 0 (1-2 days): stdlib MCP-over-HTTP spike against the real chad
  client (cursor pagination, session header, `protocolVersion` echo,
  notification `202`, no-GET-stream `405`, DELETE tolerance). Fallback
  decision pre-made: prod HTTP dep if the spike fails.
- Phase 1 (1-2 days): `--no-builtins`, overlay map, exclusion line,
  annotations, alias flow. Gate: existing suite green plus flag tests;
  `make gate` clean after Phase -1 resolves.
- Phase 2 (3-4 days): four tools, Zed-cap backing with fallback,
  capability matrix tests, edit surgery with pinned strings, terminal
  lifecycle with kill and timeout, bridge-side guards with attack
  tests (destructive, outside-root, dotfile deny).
- Phase 3 (3-5 days): bridge CLI (relay, spawn, clamp, id table,
  allow-once-only), three-tier harness on the official TS client with
  zod validation, chaos tests from section 8.
- Phase 4 (1-2 days): live on a warm remote (provisioning, token
  plumbing, latency pass), docs and CHANGELOG close.

## 10. Open questions for the operator

- Remote Apple Silicon Mac plus key-based ssh: ready?
- Primary backing Zed-capability with local fallback: confirmed?
- `read` defaults (whole file capped) acceptable, or line windows?
- Workspace-root-deny strictness for v1: keep strict?
