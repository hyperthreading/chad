#!/usr/bin/env node
// Three-tier harness: official TS client -> `chad acp-bridge --test-remote`
// -> scripted `chad acp --test-agent` as the remote peer (no ssh, stdio).
// Exercises the relay genuinely: init/new/prompt, permission mapping,
// update forwarding, cancel. Run: `npm run test:bridge`.
import { spawn } from "node:child_process";
import { Writable, Readable } from "node:stream";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import * as acp from "@zed-industries/agent-client-protocol";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
let failures = 0;

function check(name, cond, detail = "") {
  if (cond) {
    console.log("  ok: " + name);
  } else {
    failures++;
    console.log("  FAIL: " + name + (detail ? " -- " + detail : ""));
  }
}

class BridgeClient {
  constructor() {
    this.updates = [];
    this.permTitles = [];
  }
  async requestPermission(params) {
    this.permTitles.push(params.toolCall.title);
    return { outcome: { outcome: "selected", optionId: "allow-once" } };
  }
  async sessionUpdate(params) {
    this.updates.push(params.update);
  }
  async writeTextFile() {
    return {};
  }
  async readTextFile() {
    return { content: "" };
  }
}

function launchBridge(script) {
  const proc = spawn("uv", ["run", "chad", "acp-bridge", "--test-remote", script], {
    cwd: REPO,
    stdio: ["pipe", "pipe", "inherit"],
  });
  const input = Writable.toWeb(proc.stdin);
  const output = Readable.toWeb(proc.stdout);
  return { proc, stream: acp.ndJsonStream(input, output) };
}

async function scenarioRelay() {
  console.log("scenario: relay approve end to end");
  const { proc, stream } = launchBridge("tool");
  try {
    const client = new BridgeClient();
    const conn = new acp.ClientSideConnection(() => client, stream);
    const init = await conn.initialize({
      protocolVersion: acp.PROTOCOL_VERSION,
      clientCapabilities: {},
    });
    check("protocol version 1", init.protocolVersion === 1);
    const cwd = mkdtempSync(join(tmpdir(), "chad-bridge-"));
    const created = await conn.newSession({ cwd, mcpServers: [] });
    check("session id issued", typeof created.sessionId === "string");
    const done = await conn.prompt({
      sessionId: created.sessionId,
      prompt: [{ type: "text", text: "go" }],
    });
    check("stopReason end_turn", done.stopReason === "end_turn", done.stopReason);
    check("remote permission relayed", client.permTitles.length === 1,
      JSON.stringify(client.permTitles));
    const kinds = client.updates.map((u) => u.sessionUpdate);
    check("tool_call forwarded", kinds.includes("tool_call"));
    check("tool_call_update forwarded", kinds.includes("tool_call_update"));
    const blob = JSON.stringify(client.updates);
    // The scripted remote only reports success when its permission round
    // trip grants allow-once; a flat (unenveloped) answer denies instead.
    check("tool ran (permission granted)", blob.includes("[exit 0]"), blob.slice(-300));
    check("agent text forwarded", kinds.includes("agent_message_chunk"));
  } finally {
    proc.kill();
  }
}

async function scenarioRelayCancel() {
  console.log("scenario: relay cancel");
  const { proc, stream } = launchBridge("slow");
  try {
    const client = new BridgeClient();
    const conn = new acp.ClientSideConnection(() => client, stream);
    await conn.initialize({ protocolVersion: acp.PROTOCOL_VERSION, clientCapabilities: {} });
    const cwd = mkdtempSync(join(tmpdir(), "chad-bridge-"));
    const created = await conn.newSession({ cwd, mcpServers: [] });
    const pending = conn.prompt({
      sessionId: created.sessionId,
      prompt: [{ type: "text", text: "take your time" }],
    });
    await new Promise((resolve) => setTimeout(resolve, 500));
    await conn.cancel({ sessionId: created.sessionId });
    const done = await pending;
    check("stopReason cancelled", done.stopReason === "cancelled", done.stopReason);
  } finally {
    proc.kill();
  }
}

await scenarioRelay();
await scenarioRelayCancel();
if (failures > 0) {
  console.log(failures + " check(s) FAILED");
  process.exit(1);
}
console.log("all bridge checks passed");
