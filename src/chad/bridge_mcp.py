"""Local-side MCP tools for the ACP bridge (`chad acp-bridge`).

A tiny stdlib-only MCP server (no new production dependency: uvicorn is
dev-only) exposing four tools the remote brain calls instead of builtins:
read, write, edit, bash. Each tool has two backings. When the Zed session
advertised the matching client capability, the call goes to Zed (`fs/*`,
`terminal/*`: native diff, buffer coherence, terminal panels come free).
Otherwise it runs locally with the same functions the agent loop uses.

Containment is enforced here, on both backings, before anything runs:
paths resolve against the session root and stay inside it, protected names
refuse, destructive shell patterns refuse. The remote name-keyed screens
cannot see these calls (different names, different tree), so this module
is the screen. Live use stays off until this file and the bearer land.
"""

import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from . import guardrails, seatbelt
from .tools import JsonValue, is_json_object, tool_bash, tool_edit, tool_write

_RESULT_CLIP = 20000
_TERMINAL_BYTE_LIMIT = 131072
_TERMINAL_POLL_S = 0.5

_DENY_COMPONENTS = frozenset({
    ".ssh", ".aws", ".gnupg", ".pki", ".docker", ".env", ".env.local",
    ".netrc", ".vault-token",
})
_DENY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx")

TOOL_DEFS = [
    {
        "name": "read",
        "description": "Read a file. Paths may be absolute or relative to the project working directory. Returns numbered lines.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "description": "First line, 1-based."},
                "limit": {"type": "integer", "description": "Max lines."},
            },
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": True},
    },
]
TOOL_DEFS.extend([
    {
        "name": "write",
        "description": "Write (create or overwrite) a file with the given content. Paths may be absolute or relative to the project working directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit",
        "description": "Replace a unique substring in a file with new text. Requires an EXACT match of old including indentation. Paths may be absolute or relative to the project working directory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "bash",
        "description": "Run a shell command in the project working directory and return combined stdout/stderr.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)."},
            },
            "required": ["command"],
        },
    },
])


def check_path(root: str, raw: JsonValue) -> tuple:
    """Resolve a tool path against the session root, contained. v1 is strict:
outside the root hard-denies, protected names refuse, no exceptions yet."""
    if not isinstance(raw, str) or not raw.strip():
        return False, "[denied: empty path]"
    base = os.path.realpath(root)
    joined = raw if os.path.isabs(raw) else os.path.join(base, raw)
    ap = os.path.realpath(joined)
    if ap != base and not ap.startswith(base + os.sep):
        return False, "[denied: path is outside the workspace: " + raw + "]"
    rel = ap[len(base):].strip(os.sep)
    for part in rel.split(os.sep):
        low = part.lower()
        if low in _DENY_COMPONENTS or low.endswith(_DENY_SUFFIXES):
            return False, "[denied: protected path: " + raw + "]"
    return True, ap


def destructive_note(command: str) -> str | None:
    """Mirror the local catastrophic-command screen (bridge must re-screen:
    the remote name-keyed guard cannot see these calls).""" 
    if guardrails.is_destructive_bash(command):
        return ("[blocked destructive command: matches a catastrophic pattern "
                "(recursive delete of a filesystem root, top-level directory, "
                "or home tree, or mkfs / dd-to-device / curl|sh). Re-issue it "
                "with a narrower target.]")
    return None


def shape_read(display: str, text: str, offset=None, limit=None) -> str:
    """Numbered-line view with offset/limit and a char cap (both backings).""" 
    try:
        start = int(offset) if offset is not None else 1
    except (TypeError, ValueError):
        start = 1
    if start < 1:
        start = 1
    try:
        count = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        count = None
    if count is not None and count < 0:
        count = None
    lines = text.splitlines()
    picked = lines[start - 1:]
    if count is not None:
        picked = picked[:count]
    body = "\n".join("%6d\t%s" % (i, ln) for i, ln in enumerate(picked, start))
    if not body:
        return "[empty file]"
    if len(body) > _RESULT_CLIP:
        return body[:_RESULT_CLIP] + "\n[... truncated ...]"
    return body


def apply_edit_text(data: str, old: str, new: str) -> tuple:
    """Pure unique-replace for the Zed backing (exact chad messages). The
    unescape recovery cascade stays local-only for v1; the miss text is
    plain so the model still self-corrects.""" 
    if old == new:
        return False, ("[no-op edit: old and new are identical; change the "
                       "content or stop]")
    n = data.count(old)
    if n == 1:
        return True, data.replace(old, new, 1)
    if n > 1:
        return False, ("[old string appears %d times; make it unique by "
                       "including more surrounding lines]" % n)
    return False, ("[old string not found; copy it exactly from what you "
                   "just read]")


def _clip(text: str) -> str:
    if len(text) <= _RESULT_CLIP:
        return text
    return text[:_RESULT_CLIP] + "\n[... truncated ...]"


class LocalMCPServer:
    """Loopback MCP server for one bridge session (Zed-cap first, local fallback).

    root is the Zed session cwd: every path resolves and stays inside it.
    zed_caller(method, params) answers Zed capability calls as (ok, result);
    None (or missing caps) means run locally. caps selects per tool:
    {"read": bool, "write": bool, "terminal": bool}. active_session fills
    the sessionId Zed requires; the bridge sets it around the remote turn
    (prompts are serialized, so one slot is exact). The bearer is random per
    server; the bridge injects it into the overlay URL headers.
    """

    def __init__(self, root: str, zed_caller=None, caps=None, bearer=None,
                 bind: bool = True):
        self.root = os.path.realpath(root)
        self.zed_caller = zed_caller
        self.caps = dict(caps) if isinstance(caps, dict) else {}
        self.active_session = None
        self.bearer = bearer or secrets.token_hex(16)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port = 0
        if bind:
            self._httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                              self._handler())
            self.port = self._httpd.server_address[1]
            self._thread = threading.Thread(
                target=self._httpd.serve_forever, daemon=True)
            self._thread.start()

    def url(self) -> str:
        return "http://127.0.0.1:%d/mcp" % self.port

    def auth_headers(self) -> dict:
        return {"Authorization": "Bearer " + self.bearer}

    def close(self) -> None:
        if self._httpd is None or self._thread is None:
            return
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        self._thread.join(timeout=5)

    def handle_message(self, message, auth: str) -> tuple:
        """Answer one decoded MCP message; returns (http_code, body|None).

        Shared by the standalone socket and the bridge hub router, so both
        speak exactly one protocol dialect.
        """
        if not is_json_object(message):
            return 400, {"error": "not an object"}
        if not hmac.compare_digest(auth, "Bearer " + self.bearer):
            return 401, {"error": "unauthorized"}
        method = message.get("method")
        if "id" not in message:
            return 202, None
        msgid = message["id"]
        params = message.get("params")
        parsed = params if is_json_object(params) else {}
        if method == "initialize":
            version = parsed.get("protocolVersion")
            return 200, {"jsonrpc": "2.0", "id": msgid, "result": {
                "protocolVersion": version if isinstance(version, str)
                else "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "chad-bridge", "version": "1.0"}}}
        if method == "tools/list":
            return 200, {"jsonrpc": "2.0", "id": msgid, "result": {
                "tools": TOOL_DEFS}}
        if method == "tools/call":
            name = parsed.get("name")
            args = parsed.get("arguments")
            text, failed = self.call_tool(
                name if isinstance(name, str) else "",
                args if is_json_object(args) else {})
            return 200, {"jsonrpc": "2.0", "id": msgid, "result": {
                "content": [{"type": "text", "text": text}],
                "isError": failed}}
        return 200, {"jsonrpc": "2.0", "id": msgid, "error": {
            "code": -32601, "message": "method not found: " + str(method)}}

    def _handler(self):
        server = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return None

            def _reply(self, code: int, obj=None):
                body = b"" if obj is None else json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                self._reply(405, {"error": "method not allowed"})

            def do_DELETE(self):
                self._reply(405, {"error": "method not allowed"})

            def do_POST(self):
                if self.path != "/mcp":
                    self._reply(404, {"error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    length = 0
                try:
                    message = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._reply(400, {"error": "invalid JSON"})
                    return
                if not is_json_object(message):
                    self._reply(400, {"error": "not an object"})
                    return
                provided = self.headers.get("Authorization") or ""
                code, body = server.handle_message(message, provided)
                self._reply(code, body)

        return _Handler

    def call_tool(self, name: str, args: dict) -> tuple:
        """Run one tool; returns (text, is_error). Never raises."""
        try:
            if name == "read":
                return self._t_read(args)
            if name == "write":
                return self._t_write(args)
            if name == "edit":
                return self._t_edit(args)
            if name == "bash":
                return self._t_bash(args)
            return "[unknown tool %r]" % (name,), True
        except Exception as e:
            return "[bridge tool failed: %s: %s]" % (type(e).__name__, e), True

    def _wants_zed(self, kind: str) -> bool:
        return bool(self.caps.get(kind)) and self.zed_caller is not None

    def _zed(self, method: str, params: dict):
        """Call one Zed capability; returns (ok, result-or-error-text)."""
        if self.active_session is None:
            return False, "no active session"
        body = dict(params)
        body["sessionId"] = self.active_session
        try:
            ok, result = self.zed_caller(method, body)
        except Exception as e:
            return False, "zed call failed: %s: %s" % (type(e).__name__, e)
        if not ok:
            return False, str(result)
        return True, result

    def _rel(self, abspath: str) -> str:
        try:
            return os.path.relpath(abspath, self.root)
        except ValueError:
            return abspath

    def _read_text(self, loc: str):
        """Fetch file text via Zed when capable, else local disk."""
        if self._wants_zed("read"):
            ok, result = self._zed("fs/read_text_file", {"path": loc})
            if not ok:
                return False, "[Zed read failed: %s]" % (result,)
            if not is_json_object(result):
                return False, "[Zed read failed: bad response]"
            content = result.get("content")
            if not isinstance(content, str):
                return False, "[Zed read failed: bad response]"
            return True, content
        if not os.path.isfile(loc):
            return False, "[no such file: %s]" % (loc,)
        try:
            with open(loc, encoding="utf-8") as f:
                return True, f.read()
        except UnicodeDecodeError:
            return False, "[cannot read %s: not UTF-8 text]" % (loc,)
        except OSError as e:
            return False, "[cannot read %s: %s]" % (loc, e)

    def _t_read(self, args: dict) -> tuple:
        ok, loc = check_path(self.root, args.get("path"))
        if not ok:
            return loc, True
        good, text = self._read_text(loc)
        if not good:
            return text, True
        return shape_read(self._rel(loc), text, args.get("offset"),
                          args.get("limit")), False

    def _t_write(self, args: dict) -> tuple:
        path = args.get("path")
        content = args.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return "[write needs a path and content]", True
        ok, loc = check_path(self.root, path)
        if not ok:
            return loc, True
        if self._wants_zed("write"):
            good, result = self._zed(
                "fs/write_text_file", {"path": loc, "content": content})
            if not good:
                return "[Zed write failed: %s]" % (result,), True
            return "[wrote %d bytes to %s]" % (len(content), self._rel(loc)), False
        try:
            return tool_write(loc, content), False
        except OSError as e:
            return "[write failed: %s]" % (e,), True

    def _t_edit(self, args: dict) -> tuple:
        path = args.get("path")
        old = args.get("old")
        new = args.get("new")
        if not isinstance(path, str) or not isinstance(old, str) \
                or not isinstance(new, str):
            return "[edit needs a path, old, and new]", True
        ok, loc = check_path(self.root, path)
        if not ok:
            return loc, True
        if self._wants_zed("write") and self._wants_zed("read"):
            # Both caps or neither: reading local disk while Zed owns the
            # buffer would operate on stale text.
            good, text = self._read_text(loc)
            if not good:
                return text, True
            changed, out = apply_edit_text(text, old, new)
            if not changed:
                return out, True
            good, result = self._zed(
                "fs/write_text_file", {"path": loc, "content": out})
            if not good:
                return "[Zed write failed: %s]" % (result,), True
            return "[edited %s]" % (self._rel(loc),), False
        try:
            return tool_edit(loc, old, new), False
        except OSError as e:
            return "[edit failed: %s]" % (e,), True

    def _t_bash(self, args: dict) -> tuple:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return "[bash needs a command]", True
        note = destructive_note(command)
        if note is not None:
            return note, True
        try:
            timeout = int(args.get("timeout", 120))
        except (TypeError, ValueError):
            timeout = 120
        if timeout <= 0:
            timeout = 120
        if self._wants_zed("terminal"):
            return self._bash_zed(command, timeout)
        seatbelt.set_context(False, self.root)
        try:
            return tool_bash(command, timeout, None), False
        except Exception as e:
            return "[bash failed: %s: %s]" % (type(e).__name__, e), True
        finally:
            seatbelt.set_context(False, None)

    def _bash_zed(self, command: str, timeout: int) -> tuple:
        ok, result = self._zed("terminal/create", {
            "command": "/bin/sh", "args": ["-c", command],
            "cwd": self.root, "outputByteLimit": _TERMINAL_BYTE_LIMIT})
        if not ok:
            return "[Zed terminal failed: %s]" % (result,), True
        if not is_json_object(result):
            return "[Zed terminal failed: bad response]", True
        terminal_id = result.get("terminalId")
        if not isinstance(terminal_id, str):
            return "[Zed terminal failed: bad response]", True
        try:
            deadline = time.monotonic() + timeout
            output = ""
            status = None
            while True:
                good, res = self._zed(
                    "terminal/output", {"terminalId": terminal_id})
                if good and is_json_object(res):
                    text = res.get("output")
                    if isinstance(text, str):
                        output = text
                    exit_status = res.get("exitStatus")
                    if is_json_object(exit_status):
                        status = exit_status
                        break
                if time.monotonic() >= deadline:
                    self._zed("terminal/kill", {"terminalId": terminal_id})
                    good, res = self._zed(
                        "terminal/output", {"terminalId": terminal_id})
                    if good and is_json_object(res):
                        text = res.get("output")
                        if isinstance(text, str):
                            output = text
                    return ("[timed out after %ds]\n%s" % (timeout, _clip(output)),
                            True)
                time.sleep(_TERMINAL_POLL_S)
            code = status.get("exitCode")
            if isinstance(code, bool):
                code = None
            if isinstance(code, int):
                return ("[exit %d]\n%s" % (code, _clip(output)), code != 0)
            signal = status.get("signal")
            if isinstance(signal, str):
                return ("[terminated by signal %s]\n%s" % (signal, _clip(output)),
                        True)
            return (_clip(output), False)
        finally:
            self._zed("terminal/release", {"terminalId": terminal_id})


class BridgeMCPHub:
    """One loopback HTTP server routing /<sid>/mcp to per-session tool states.

    The bridge multiplexes Zed sessions over a single ssh -R forward, so
    per-session MCP servers cannot each bind a port known at ssh-spawn
    time. Instead each session gets a LocalMCPServer in unbound mode
    (no socket of its own) filed under its quoted session id; this hub is
    the only socket and dispatches by path. Unknown or retired sessions
    answer 404, so a stale remote never touches another tree.
    """

    def __init__(self):
        self._sessions = {}
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()

    def add_session(self, sid: str, root: str, zed_caller=None,
                    caps=None) -> object:
        """Create the tool state for one bridge session; returns the server."""
        from urllib.parse import quote
        server = LocalMCPServer(root, zed_caller=zed_caller, caps=caps,
                                bind=False)
        with self._lock:
            self._sessions[quote(sid, safe="")] = server
        return server

    def drop_session(self, sid: str) -> None:
        from urllib.parse import quote
        with self._lock:
            self._sessions.pop(quote(sid, safe=""), None)

    def close(self) -> None:
        try:
            self._httpd.shutdown()
        except Exception:
            pass
        try:
            self._httpd.server_close()
        except Exception:
            pass
        self._thread.join(timeout=5)

    def _handler(self):
        hub = self

        class _HubHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return None

            def _reply(self, code: int, obj=None):
                body = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _route(self):
                from urllib.parse import unquote
                path = self.path
                if not path.endswith("/mcp"):
                    return None
                head = path[: -len("/mcp")]
                if not head.startswith("/") or len(head) < 2:
                    return None
                with hub._lock:
                    return hub._sessions.get(unquote(head[1:]))

            def do_GET(self):
                self._reply(405, {"error": "method not allowed"})

            def do_DELETE(self):
                self._reply(405, {"error": "method not allowed"})

            def do_POST(self):
                server = self._route()
                if server is None:
                    self._reply(404, {"error": "unknown session"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except (TypeError, ValueError):
                    length = 0
                try:
                    message = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    self._reply(400, {"error": "invalid JSON"})
                    return
                provided = self.headers.get("Authorization") or ""
                code, body = server.handle_message(message, provided)
                self._reply(code, body)

        return _HubHandler
