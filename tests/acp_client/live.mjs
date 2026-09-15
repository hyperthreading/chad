#!/usr/bin/env node
// Live smoke test: real `chad acp` (weights, no --test-agent) driven by the
// official TypeScript client. One tiny scoped task in a fresh tmp workspace.
// Permissions auto-approve once. Run: `node live.mjs` (repo root as agent cwd).
// Overall budget 25 minutes; exits non-zero on failure or timeout.
import { spawn } from "node:child_process";
import { Writable, Readable } from "node:stream";
import { mkdtempSync, existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import * as acp from "@zed-industries/agent-client-protocol";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const BUDGET_MS = 25 * 60 * 1000;
setTimeout(() => { console.log("TIMEOUT: budget exhausted"); process.exit(2); }, BUDGET_MS).unref();

class LiveClient {
  constructor() { this.updates = []; }
  async requestPermission(params) {
    console.log("PERMISSION: " + params.toolCall.title);
    return { outcome: { outcome: "selected", optionId: "allow-once" } };
  }
  async sessionUpdate(params) {
    const u = params.update;
    this.updates.push(u);
    if (u.sessionUpdate === "agent_message_chunk") process.stdout.write(u.content.text);
    else if (u.sessionUpdate === "tool_call") console.log("\n[tool] " + u.title);
    else if (u.sessionUpdate === "tool_call_update") console.log("\n[tool done] " + u.status);
    else console.log("\n[" + u.sessionUpdate + "]");
  }
  async writeTextFile() { return {}; }
  async readTextFile() { return { content: "" }; }
}

const proc = spawn("uv", ["run", "chad", "acp"], { cwd: REPO, stdio: ["pipe", "pipe", "inherit"] });
const conn = new acp.ClientSideConnection(
  () => new LiveClient(),
  acp.ndJsonStream(Writable.toWeb(proc.stdin), Readable.toWeb(proc.stdout)));

const init = await conn.initialize({ protocolVersion: acp.PROTOCOL_VERSION, clientCapabilities: {} });
console.log("\ninit protocol: " + init.protocolVersion);
const work = mkdtempSync(join(tmpdir(), "chad-acp-live-"));
console.log("workspace: " + work);
const created = await conn.newSession({ cwd: work, mcpServers: [] });
console.log("session: " + created.sessionId);
const done = await conn.prompt({
  sessionId: created.sessionId,
  prompt: [{ type: "text", text: "Create a file hello_acp.txt in the working directory containing exactly this one line: hello from chad over acp" }],
});
console.log("\nstopReason: " + done.stopReason);
const target = join(work, "hello_acp.txt");
if (existsSync(target)) {
  console.log("file content: " + JSON.stringify(readFileSync(target, "utf8")));
  console.log(done.stopReason === "end_turn" ? "LIVE PASS" : "LIVE WEAK (file landed, odd stop)");
} else {
  console.log("LIVE FAIL (file missing)");
  process.exitCode = 1;
}
proc.kill();
process.exit();
