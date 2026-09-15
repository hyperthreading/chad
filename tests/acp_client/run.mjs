#!/usr/bin/env node
// Official-client conformance harness for `chad acp`.
//
// Drives the real stdio transport with the official TypeScript client
// (@zed-industries/agent-client-protocol). Every agent message is validated
// by the SDKs zod schemas on receipt, so malformed protocol output fails
// loudly here instead of confusing Zed. Uses only the scripted test agents
// (`chad acp --test-agent ...`): no weights, no network, hermetic.
//
// Run: `npm install && npm test` in this directory (repo root is the agent cwd).
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

function updateKinds(updates, kind) {
  return updates.filter((u) => u.sessionUpdate === kind);
}

class HarnessClient {
  constructor(permission) {
    this.permission = permission;
    this.updates = [];
    this.permRequests = 0;
  }
  async requestPermission(params) {
    this.permRequests++;
    return { outcome: { outcome: "selected", optionId: this.permission } };
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

function launch(script) {
  const proc = spawn("uv", ["run", "chad", "acp", "--test-agent", script], {
    cwd: REPO,
    stdio: ["pipe", "pipe", "inherit"],
  });
  const input = Writable.toWeb(proc.stdin);
  const output = Readable.toWeb(proc.stdout);
  return { proc, stream: acp.ndJsonStream(input, output) };
}

async function scenarioAllow() {
  console.log("scenario: approve permission");
  const { proc, stream } = launch("tool");
  try {
    const client = new HarnessClient("allow-once");
    const conn = new acp.ClientSideConnection(() => client, stream);
    const init = await conn.initialize({
      protocolVersion: acp.PROTOCOL_VERSION,
      clientCapabilities: {},
    });
    check("protocol version 1", init.protocolVersion === 1);
    const cwd = mkdtempSync(join(tmpdir(), "chad-acp-"));
    const created = await conn.newSession({ cwd, mcpServers: [] });
    check("session id issued", typeof created.sessionId === "string");
    const done = await conn.prompt({
      sessionId: created.sessionId,
      prompt: [{ type: "text", text: "go" }],
    });
    check("stopReason end_turn", done.stopReason === "end_turn", done.stopReason);
    check("one permission question", client.permRequests === 1);
    const calls = updateKinds(client.updates, "tool_call");
    check("tool_call announced", calls.length === 1, JSON.stringify(calls.length));
    check("tool title", calls[0] && calls[0].title === "Run echo hi");
    const finished = updateKinds(client.updates, "tool_call_update");
    check("tool completed", finished[0] && finished[0].status === "completed");
    const chunks = updateKinds(client.updates, "agent_message_chunk");
    check("final text streamed", chunks.some((c) => c.content.text.includes("All done.")));
  } finally {
    proc.kill();
  }
}

async function scenarioDeny() {
  console.log("scenario: reject permission");
  const { proc, stream } = launch("tool");
  try {
    const client = new HarnessClient("reject-once");
    const conn = new acp.ClientSideConnection(() => client, stream);
    await conn.initialize({ protocolVersion: acp.PROTOCOL_VERSION, clientCapabilities: {} });
    const cwd = mkdtempSync(join(tmpdir(), "chad-acp-"));
    const created = await conn.newSession({ cwd, mcpServers: [] });
    const done = await conn.prompt({
      sessionId: created.sessionId,
      prompt: [{ type: "text", text: "go" }],
    });
    check("stopReason end_turn", done.stopReason === "end_turn", done.stopReason);
    const finished = updateKinds(client.updates, "tool_call_update");
    check("tool failed", finished[0] && finished[0].status === "failed");
  } finally {
    proc.kill();
  }
}

async function scenarioCancel() {
  console.log("scenario: cancel mid-prompt");
  const { proc, stream } = launch("slow");
  try {
    const client = new HarnessClient("allow-once");
    const conn = new acp.ClientSideConnection(() => client, stream);
    await conn.initialize({ protocolVersion: acp.PROTOCOL_VERSION, clientCapabilities: {} });
    const cwd = mkdtempSync(join(tmpdir(), "chad-acp-"));
    const created = await conn.newSession({ cwd, mcpServers: [] });
    const pending = conn.prompt({
      sessionId: created.sessionId,
      prompt: [{ type: "text", text: "take your time" }],
    });
    await new Promise((resolve) => setTimeout(resolve, 300));
    await conn.cancel({ sessionId: created.sessionId });
    const done = await pending;
    check("stopReason cancelled", done.stopReason === "cancelled", done.stopReason);
  } finally {
    proc.kill();
  }
}

async function scenarioBadSession() {
  console.log("scenario: unknown session errors");
  const { proc, stream } = launch("echo");
  try {
    const client = new HarnessClient("allow-once");
    const conn = new acp.ClientSideConnection(() => client, stream);
    await conn.initialize({ protocolVersion: acp.PROTOCOL_VERSION, clientCapabilities: {} });
    let errText = "";
    try {
      await conn.prompt({ sessionId: "missing", prompt: [{ type: "text", text: "hi" }] });
    } catch (err) {
      errText = String(err);
    }
    check("prompt errors loudly", errText.length > 0, "expected a JSON-RPC error");
  } finally {
    proc.kill();
  }
}

await scenarioAllow();
await scenarioDeny();
await scenarioCancel();
await scenarioBadSession();
if (failures > 0) {
  console.log(failures + " check(s) FAILED");
  process.exit(1);
}
console.log("all harness checks passed");
