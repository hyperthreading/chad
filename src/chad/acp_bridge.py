"""ACP bridge: Zed local, full chad remote (`chad acp-bridge`).

Zed --ACP/stdio--> this bridge --ACP/ssh-stdio--> remote
`chad acp --no-builtins local`, plus MCP/ssh-forward back: the bridge
serves per-session `local` MCP tools (bridge_mcp.py) that the remote
model calls instead of builtins.

The bridge holds no agent loop. It relays prompts, streams updates,
relays permission (allow-once only, never allow-always), relays cancel
and modes (remote yolo clamped to auto), and maps stop reasons through.
Tool execution with workspace containment lives in bridge_mcp.py.
"""

import json
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from . import bridge_mcp
from .acp import AcpServer
from .tools import JsonValue, is_json_object

REMOTE_PORT_DEFAULT = 18789


class _BridgeError(Exception):
    """Remote-side or transport failure answering a Zed prompt."""


def clamp_mode(mode: str) -> str:
    """Remote yolo would auto-approve MCP tools silently and mute the relay."""
    return "auto" if mode == "yolo" else mode


def map_permission_answer(res: JsonValue) -> dict:
    """Map a Zed permission outcome onto the remote option ids.

    Zed echoes one of the offered ids when it plays along; anything
    foreign falls back to an allow-substring heuristic, defaulting to
    reject. allow-always is never relayed: standing approval across ssh
    is indefensible, so the answer is allow-once at most. Returns the
    inner outcome object; callers nest it under `outcome` to form the
    full permission-response shape.
    """
    cancelled = {"outcome": "cancelled"}
    deny = {"outcome": "selected", "optionId": "reject-once"}
    allow = {"outcome": "selected", "optionId": "allow-once"}
    if not is_json_object(res):
        return dict(deny)
    outcome = res.get("outcome")
    if not is_json_object(outcome):
        return dict(deny)
    if outcome.get("outcome") == "cancelled":
        return dict(cancelled)
    if outcome.get("outcome") != "selected":
        return dict(deny)
    option = outcome.get("optionId")
    if option in ("allow-once", "reject-once"):
        return {"outcome": "selected", "optionId": str(option)}
    if isinstance(option, str) and "allow" in option.lower():
        return dict(allow)
    return dict(deny)


def caps_for(client_caps: dict) -> dict:
    """Reduce Zed clientCapabilities to per-tool backing flags."""
    fs = client_caps.get("fs")
    fs = fs if is_json_object(fs) else {}
    return {
        "read": bool(fs.get("readTextFile", False)),
        "write": bool(fs.get("writeTextFile", False)),
        "terminal": bool(client_caps.get("terminal", False)),
    }


class RemoteDriver:
    """Minimal ACP client toward the remote agent over piped stdio.

    One flight at a time (the bridge serializes prompts like the local
    adapter does). The reader thread answers remote permission requests
    through on_permission and forwards updates through on_update; prompt()
    blocks its caller thread with should_stop polling for cancel.
    """

    def __init__(self, readline, writeline):
        self._readline = readline
        self._writeline = writeline
        self._send_lock = threading.Lock()
        self._pending: dict[int, _RemotePending] = {}
        self._pending_lock = threading.Lock()
        self._seq = 0
        self._dead = False
        self.on_permission = None
        self.on_update = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _send(self, message: dict) -> None:
        line = json.dumps(message)
        with self._send_lock:
            self._writeline(line)

    def call(self, method: str, params: dict, timeout=None):
        """Blocking request; returns (True, result) or (False, error-text)."""
        with self._pending_lock:
            self._seq += 1
            rid = self._seq
            pending = _RemotePending()
            self._pending[rid] = pending
        try:
            self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params})
        except Exception as e:
            with self._pending_lock:
                self._pending.pop(rid, None)
            return False, "remote gone: %s" % (e,)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._dead:
                break
            if deadline is None:
                done = pending.event.wait(0.05)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                done = pending.event.wait(min(0.05, remaining))
            if done:
                break
        with self._pending_lock:
            self._pending.pop(rid, None)
        if self._dead:
            return False, "remote process ended"
        if pending.event.is_set():
            if pending.error is not None:
                return False, str(pending.error)
            return True, pending.result
        return False, "timed out waiting for the remote"

    def notify(self, method: str, params: dict) -> None:
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})
        except Exception:
            self._dead = True

    def _read_loop(self) -> None:
        while True:
            try:
                line = self._readline()
            except Exception:
                break
            if not line:
                break
            text = line.strip()
            if text:
                self._handle_line(text)
        self._dead = True
        with self._pending_lock:
            for pending in self._pending.values():
                pending.event.set()

    def _handle_line(self, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not is_json_object(message):
            return
        method = message.get("method")
        if isinstance(method, str):
            params = message.get("params")
            parsed = params if is_json_object(params) else {}
            if "id" in message:
                self._answer_request(message["id"], method, parsed)
            elif method == "session/update":
                update = parsed.get("update")
                if is_json_object(update) and self.on_update is not None:
                    try:
                        self.on_update(update)
                    except Exception:
                        pass
            return
        if "id" in message:
            rid = message["id"]
            if isinstance(rid, bool) or not isinstance(rid, int):
                return
            with self._pending_lock:
                pending = self._pending.get(rid)
            if pending is None:
                return
            error = message.get("error")
            if is_json_object(error):
                detail = error.get("message")
                pending.error = str(detail) if isinstance(detail, str) \
                    else "remote error"
            elif "result" in message:
                pending.result = message["result"]
            else:
                pending.error = "empty response"
            pending.event.set()

    def _answer_request(self, rid, method: str, params: dict) -> None:
        if method != "session/request_permission" or self.on_permission is None:
            self._send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32601, "message": "unknown method: " + method}})
            return
        tool_call = params.get("toolCall")
        options = params.get("options")
        try:
            answer = self.on_permission(
                tool_call if is_json_object(tool_call) else {},
                options if isinstance(options, list) else [])
        except Exception as e:
            answer = {"outcome": "cancelled"}
            sys.stderr.write("chad acp-bridge: permission handler failed: %r\n" % (e,))
        self._send({"jsonrpc": "2.0", "id": rid, "result": answer})

    def initialize(self, caps: dict):
        return self.call("initialize", {
            "protocolVersion": 1, "clientCapabilities": caps}, timeout=30)

    def new_session(self, cwd: str, servers: list):
        ok, result = self.call(
            "session/new", {"cwd": cwd, "mcpServers": servers}, timeout=120)
        if not ok:
            raise _BridgeError(result)
        if not is_json_object(result):
            raise _BridgeError("bad new_session response")
        session_id = result.get("sessionId")
        if not isinstance(session_id, str):
            raise _BridgeError("bad new_session response")
        return session_id

    def set_mode(self, remote_sid: str, mode: str) -> None:
        ok, result = self.call(
            "session/set_mode",
            {"sessionId": remote_sid, "modeId": clamp_mode(mode)},
            timeout=30)
        if not ok:
            sys.stderr.write("chad acp-bridge: remote set_mode failed: %s\n"
                             % (result,))

    def prompt(self, remote_sid: str, text: str, on_update,
               on_permission, should_stop) -> str:
        """Run one remote turn. Returns its stop reason; raises _BridgeError."""
        self.on_update = on_update
        self.on_permission = on_permission
        try:
            with self._pending_lock:
                self._seq += 1
                rid = self._seq
                pending = _RemotePending()
                self._pending[rid] = pending
            try:
                self._send({"jsonrpc": "2.0", "id": rid, "method":
                            "session/prompt",
                            "params": {"sessionId": remote_sid, "prompt": [
                                {"type": "text", "text": text}]}})
            except Exception as e:
                with self._pending_lock:
                    self._pending.pop(rid, None)
                raise _BridgeError("remote gone: %s" % (e,))
            cancelled = False
            while True:
                if self._dead:
                    raise _BridgeError("remote process ended")
                if should_stop is not None and should_stop():
                    if not cancelled:
                        cancelled = True
                        self.notify("session/cancel", {"sessionId": remote_sid})
                if pending.event.wait(0.05):
                    break
            with self._pending_lock:
                self._pending.pop(rid, None)
            if pending.error is not None:
                raise _BridgeError(str(pending.error))
            result = pending.result
            if not is_json_object(result):
                raise _BridgeError("bad prompt response")
            stop = result.get("stopReason")
            if not isinstance(stop, str):
                raise _BridgeError("bad prompt response")
            return stop
        finally:
            self.on_update = None
            self.on_permission = None

class RemoteProxy:
    """AgentLike over a remote ACP session (bridge use).

    Content already streams to Zed through forwarded updates, so run_turn
    returns empty text and only records how the turn ended for the local
    stop mapping: interrupted on cancelled, a banked note on budgets.
    """

    def __init__(self, turn, set_mode, initial_mode: str):
        self._turn = turn
        self._set_mode = set_mode
        self._mode = initial_mode
        self.interrupted = False
        self.budget_note: str | None = None

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = value
        try:
            self._set_mode(value)
        except Exception as e:
            sys.stderr.write("chad acp-bridge: mode forward failed: %r\n" % (e,))

    def run_turn(self, user_text: str, stream=True) -> str:
        self.interrupted = False
        stop = self._turn(user_text)
        if stop == "cancelled":
            self.interrupted = True
        elif stop == "max_turn_requests":
            self.budget_note = "the remote turn stopped on its step budget"
        return ""

    def save(self) -> None:
        return None


_SSH_BASE = ["ssh", "-o", "ExitOnForwardFailure=yes", "-o", "BatchMode=yes",
             "-o", "ServerAliveInterval=30", "-q", "-o", "LogLevel=ERROR"]


def build_ssh_argv(host: str, rport: int, lport: int, env: list,
                   remote_argv: list) -> list:
    """Argv spawning remote chad with a loopback forward, shell-safe.

    The remote command travels as one shlex-quoted string (ssh joins argv
    with spaces remotely); env assignments ride the same string so values
    with spaces cannot become commands. Non-secret env only; secrets go
    over typed channels, never argv.
    """
    parts = []
    if env:
        parts.append("env")
        parts.extend(env)
    parts.extend(remote_argv)
    return _SSH_BASE + ["-R", "%d:127.0.0.1:%d" % (rport, lport), host,
            " ".join(shlex.quote(p) for p in parts)]


@dataclass
class _RemotePending:
    """One outstanding remote request (mirrors acp._Pending)."""

    event: threading.Event = field(default_factory=threading.Event)
    result: JsonValue = None
    error: str | None = None


def remote_scratch(local_sid: str) -> str:
    """Remote per-session dir, relative to the remote home.

    Kept home-relative (never tilde): the remote session/new checks the
    literal path, so provision_session resolves this against the remote
    $HOME and hands back the absolute dir."""
    safe = "".join(c for c in local_sid if c.isalnum() or c in "-_")
    return ".chad/bridge/" + (safe or "session")


def _stdin_line():
    raw = sys.stdin.buffer.readline()
    if not raw:
        return None
    return raw.decode("utf-8", "replace")


def _stdout_line(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


_SYNC_FILE_CAP = 65536
_SSH_OPTS = ["ssh", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30",
             "-q", "-o", "LogLevel=ERROR"]


def _drain_prefixed(stream) -> None:
    for line in iter(stream.readline, b""):
        try:
            sys.stderr.write("[remote] " + line.decode("utf-8", "replace"))
            sys.stderr.flush()
        except Exception:
            break


def _spawn(argv, cwd=None):
    """Start a child with piped stdio; returns (proc, readline, writeline)."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=cwd)
    drain = threading.Thread(target=_drain_prefixed, args=(proc.stderr,),
                             daemon=True)
    drain.start()
    lock = threading.Lock()

    def readline():
        raw = proc.stdout.readline()
        if not raw:
            return None
        return raw.decode("utf-8", "replace")

    def writeline(line: str) -> None:
        data = (line + "\n").encode("utf-8")
        with lock:
            proc.stdin.write(data)
            proc.stdin.flush()

    return proc, readline, writeline


def _ssh_exec(host: str, remote_cmd: str, extra_opts=None, input_bytes=None,
              timeout=30):
    """Run one short remote command; returns (returncode, out, err)."""
    argv = list(_SSH_OPTS)
    argv.extend(extra_opts or [])
    argv += [host, remote_cmd]
    try:
        done = subprocess.run(argv, input=input_bytes, capture_output=True,
                              timeout=timeout)
    except Exception as e:
        raise _BridgeError("ssh exec failed: %r" % (e,))
    return done.returncode, done.stdout, done.stderr


def provision_session(host: str, scratch: str, local_cwd: str) -> str:
    """Make the remote scratch dir and push project instruction files.

    Returns the absolute remote dir: the remote session/new checks the
    literal path (no tilde expansion), so the home-relative scratch is
    resolved against the remote $HOME here. Raises _BridgeError with the
    remote stderr attached on any failure, so a bad host, path, or sshd
    policy fails the first prompt loudly.
    """
    code, out, err = _ssh_exec(host, 'printf %s "$HOME"')
    if code != 0:
        raise _BridgeError("remote HOME lookup failed: %s" % (err.decode(
            "utf-8", "replace").strip(),))
    home = out.decode("utf-8", "replace").strip()
    if not home.startswith("/") or "\n" in home:
        raise _BridgeError("remote HOME lookup failed: %r" % (home,))
    remote_dir = home.rstrip("/") + "/" + scratch
    code, _out, err = _ssh_exec(
        host, "mkdir -p " + shlex.quote(remote_dir))
    if code != 0:
        raise _BridgeError("remote mkdir failed: %s" % (err.decode(
            "utf-8", "replace").strip(),))
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = os.path.join(local_cwd, name)
        try:
            with open(path, "rb") as f:
                blob = f.read(_SYNC_FILE_CAP + 1)
        except OSError:
            continue
        if len(blob) > _SYNC_FILE_CAP:
            continue
        code, _out, err = _ssh_exec(
            host, "cat > " + shlex.quote(remote_dir + "/" + name),
            input_bytes=blob)
        if code != 0:
            raise _BridgeError("remote push %s failed: %s" % (name, err.decode(
                "utf-8", "replace").strip()))
    return remote_dir


def run(args, host=None) -> int:
    """`chad acp-bridge` entrypoint: relay Zed ACP to a remote chad."""
    default_mode = "plan" if args.plan else ("yolo" if args.yolo else "normal")
    # The hub binds first so the ssh forward aims at a port that is really
    # listening: probing a free port up front reopened a race where the
    # forward outlived the probe and something else answered remotely.
    hub = bridge_mcp.BridgeMCPHub()
    test_remote = args.test_remote
    if test_remote is not None:
        remote_argv = ["uv", "run", "chad", "acp", "--no-builtins", "local",
                       "--test-agent", test_remote]
        provision = False
        proc, readline, writeline = _spawn(remote_argv)
        mcp_base = None
    else:
        if not args.remote:
            sys.stderr.write("chad acp-bridge: --remote USER@HOST is required\n")
            hub.close()
            return 2
        rport = args.remote_port
        ssh_argv = build_ssh_argv(args.remote, rport, hub.port,
                                  list(args.remote_env or []),
                                  ["chad", "acp", "--no-builtins", "local"])
        provision = True
        proc, readline, writeline = _spawn(ssh_argv)
        mcp_base = "http://127.0.0.1:%d" % (rport,)
    driver = RemoteDriver(readline, writeline)
    ok, _res = driver.initialize({})
    if not ok:
        sys.stderr.write("chad acp-bridge: remote initialize failed\n")
        try:
            proc.kill()
        except Exception:
            pass
        hub.close()
        return 1
    if mcp_base is None:
        mcp_base = "http://127.0.0.1:%d" % (hub.port,)
    sessions = {}
    sessions_lock = threading.Lock()

    def make_agent(cwd, mode, emit, confirm, should_stop, tool_event,
                   session_id=None):
        if session_id is None:
            raise _BridgeError("bridge sessions need ids")
        return ensure_session(session_id, cwd, mode, emit, should_stop)

    def ensure_session(local_sid, cwd, mode, emit, should_stop):
        with sessions_lock:
            if local_sid in sessions:
                return sessions[local_sid]["proxy"]
        caps = caps_for(server.client_capabilities)

        def zed_caller(method, params):
            body = dict(params)
            body["sessionId"] = local_sid
            return server.request_client(local_sid, method, body)

        mcp_server = hub.add_session(local_sid, cwd, zed_caller=zed_caller,
                                     caps=caps)
        if provision:
            scratch = remote_scratch(local_sid)
            remote_cwd = provision_session(args.remote, scratch, cwd)
        else:
            remote_cwd = cwd
        overlay = [{"name": "local", "type": "http",
                    "url": mcp_base + "/" + quote(local_sid, safe="") + "/mcp",
                    "headers": [{"name": "Authorization",
                                 "value": "Bearer " + mcp_server.bearer}]}]
        servers = overlay + server.session_client_servers(local_sid)
        remote_sid = driver.new_session(remote_cwd, servers)
        # The remote session starts in its own default mode; push ours so a
        # --plan bridge (or an early Zed set_mode) holds remotely too. The
        # clamp inside set_mode keeps a yolo start from going quiet.
        driver.set_mode(remote_sid, mode)

        def turn(text):
            mcp_server.active_session = local_sid
            try:
                return driver.prompt(
                    remote_sid, text,
                    on_update=lambda update: server.send_update(local_sid, update),
                    on_permission=lambda tool_call, options: ask_remote(
                        local_sid, tool_call, options),
                    should_stop=should_stop)
            finally:
                mcp_server.active_session = None

        def ask_remote(sid, tool_call, options):
            ok, res = server.request_client(
                sid, "session/request_permission",
                {"sessionId": sid, "toolCall": tool_call,
                 "options": options})
            if not ok:
                return {"outcome": {"outcome": "cancelled"}}
            # The driver sends this dict as the request `result`, so it
            # must be the full response shape: the mapped outcome nested
            # under `outcome`, never the bare option ids (a flat answer
            # parses as unknown and the remote safely denies it).
            return {"outcome": map_permission_answer(res)}

        def push_mode(value):
            driver.set_mode(remote_sid, value)

        proxy = RemoteProxy(turn, push_mode, mode)
        with sessions_lock:
            sessions[local_sid] = {"proxy": proxy, "remote": remote_sid,
                                   "mcp": mcp_server}
        return proxy

    server = AcpServer(_stdin_line, _stdout_line, make_agent,
                       default_mode=default_mode)
    sys.stderr.write("chad acp-bridge: relaying ACP to remote chad "
                     "(protocol traffic on stdout only)\n")
    try:
        server.serve_forever()
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        hub.close()
    return 0
