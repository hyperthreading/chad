"""Minimal stdlib MCP stdio server for live tests (no third-party deps).

Exposes one mutating tool `greet` (no readOnlyHint, so chad asks permission)
that returns a deterministic greeting. Speaks newline-delimited JSON-RPC.
"""
import json
import sys

TOOL = {
    "name": "greet",
    "description": "Greet a person by name. Has a side effect (logs the greeting).",
    "inputSchema": {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    },
}


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    mid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": req["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "greeter", "version": "1.0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}})
    elif method == "tools/call":
        args = (req.get("params") or {}).get("arguments", {})
        who = str(args.get("name", "stranger"))
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text",
                         "text": "MCP greets " + who + ": hello from the stub server"}]}})
    else:
        send({"jsonrpc": "2.0", "id": mid,
              "error": {"code": -32601, "message": "nope"}})
