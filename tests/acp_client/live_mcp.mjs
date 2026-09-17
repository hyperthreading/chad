#!/usr/bin/env node
// Live MCP test: real `chad acp` + real stub MCP server + real weights.
// Task forces the model through the client-injected MCP tool, then a file
// write so the MCP result lands on disk where we can verify it.
// Permissions auto-approve once. Run: `node live_mcp.mjs`.
import { spawn } from "node:child_process";
import { Writable, Readable } from "node:stream";
import { mkdtempSync, existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import * as acp from "@zed-industries/agent-client-protocol";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..", "..");
const BUDGET_MS = 25 * 60 * 1000;
setTimeout(() => { console.log("TIMEOUT: budget exhausted"); process.exit(2); }, BUDGET_MS).unref();

class LiveClient {
  constructor() { this.updates = []; this.perms = []; }
  async requestPermission(params) {
    this.perms.push(params.toolCall.title);
    console.log("\nPERMISSION: " + params.toolCall.title);
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
const client = new LiveClient();
const conn = new acp.ClientSideConnection(
  () => client,
  acp.ndJsonStream(Writable.toWeb(proc.stdin), Readable.toWeb(proc.stdout)));

const init = await conn.initialize({ protocolVersion: acp.PROTOCOL_VERSION, clientCapabilities: {} });
console.log("\ninit protocol: " + init.protocolVersion);
const work = mkdtempSync(join(tmpdir(), "chad-acp-mcplive-"));
console.log("workspace: " + work);
const created = await conn.newSession({
  cwd: work,
  mcpServers: [{ name: "greeter", command: "python3", args: [join(HERE, "stub_mcp_server.py")] }],
});
console.log("session: " + created.sessionId);
const done = await conn.prompt({
  sessionId: created.sessionId,
  prompt: [{ type: "text", text: "Call the greeter MCP tool with name Seoul, then write its exact reply into greet.txt in the working directory. Do not add any extra text to the file." }],
});
console.log("\nstopReason: " + done.stopReason);
const mcpCalls = client.updates.filter((u) => u.sessionUpdate === "tool_call" && u.title.includes("greeter"));
console.log("mcp tool calls seen: " + mcpCalls.length);
console.log("permission titles: " + JSON.stringify(client.perms));
const target = join(work, "greet.txt");
let pass = done.stopReason === "end_turn" && mcpCalls.length > 0 && existsSync(target);
let content = pass ? readFileSync(target, "utf8") : "";
console.log("file content: " + JSON.stringify(content));
if (pass && !content.includes("MCP greets Seoul")) pass = false;
console.log(pass ? "MCP LIVE PASS" : "MCP LIVE FAIL");
proc.kill();
process.exit(pass ? 0 : 1);
