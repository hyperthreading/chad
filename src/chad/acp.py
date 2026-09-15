"""chad as an ACP agent: `chad acp` speaks Agent Client Protocol v1 over stdio.

Framing follows the official SDK's ndJsonStream: every message is one JSON
object terminated by newline. Only stdout carries protocol traffic; logs and
model-load progress go to stderr.

Method coverage (v1): initialize, authenticate (a no-op: chad is a single-user
local agent), session/new, session/prompt, session/cancel, $/cancel_request
and session/set_mode. session/load and friends answer method-not-found: ACP
sessions live for the process lifetime, while chad's own session store still
persists every turn, so `chad -c` can pick the thread up afterwards.

Mapping: Agent.run_turn does the work. Its emit callback becomes
session/update notifications (stream and info text become agent_message_chunk,
think becomes agent_thought_chunk); the structured tool_event hook on Agent
becomes tool_call and tool_call_update; _confirm becomes
session/request_permission. stopReason is cancelled when the turn was
cancelled, max_turn_requests when the governor or step cap banked a progress
note, and end_turn otherwise.

Limits, stated plainly: one engine turn runs at a time (the MLX KV cache is a
single live object), so a second prompt while one runs answers busy; prompts
chdir into their session cwd under that same lock; concurrent sessions share
the module-global MCP and todo state, so one active session at a time is the
honest setup. Client-sent mcpServers connect like user-level config (trusted,
no project gate): the project's own .mcp.json still loads underneath, and a
client entry wins a same-name conflict.
"""

import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from . import mcp
from . import session as session_store
from .tools import JsonValue, is_json_object

PROTOCOL_VERSION = 1

_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603

_MODES = (
    ("normal", "Normal", "Ask before every edit and every command."),
    ("auto", "Auto-accept edits", "Edits land silently; commands still ask."),
    ("yolo", "Yolo", "Nothing asks. The destructive-command guard still does."),
    ("plan", "Plan mode", "Read-only: investigate and propose a plan."),
)
_MODE_IDS = frozenset(m[0] for m in _MODES)

_TOOL_KINDS = {"bash": "execute", "edit": "edit", "write": "edit"}

_FINISH_FAIL_PREFIXES = ("[denied", "[blocked", "[tool error")
_RESULT_CLIP = 4000
_DIFF_CLIP = 8000

class _RequestError(Exception):
    """A JSON-RPC error answer for the request currently handled."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _str_field(params: dict[str, JsonValue], key: str) -> str | None:
    value = params.get(key)
    return value if isinstance(value, str) else None


def _embedded_text(resource: JsonValue) -> str:
    if not is_json_object(resource):
        return "[attached resource omitted: unreadable]"
    text = resource.get("text")
    if isinstance(text, str) and text.strip():
        uri = resource.get("uri")
        head = "attached file " + str(uri) + ":\n" if isinstance(uri, str) else ""
        return "[" + head + text + "]"
    return "[attached binary resource omitted: this agent is text-only]"


def prompt_text(prompt: list) -> str:
    """Flatten ACP content blocks to the plain text run_turn consumes.

    Text blocks pass through; embedded text resources (how editors attach file
    context) are inlined; images, audio and binary blobs become honest
    placeholders so the model asks for words instead of guessing at bytes.
    Unknown block types are skipped rather than failing the whole prompt.
    """
    parts: list[str] = []
    for block in prompt:
        if not is_json_object(block):
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text)
        elif kind == "image":
            parts.append("[image content omitted: this agent is text-only " +
                         "- describe what the image shows in words]")
        elif kind == "audio":
            parts.append("[audio content omitted: this agent is text-only " +
                         "- transcribe what matters in words]")
        elif kind == "resource_link":
            uri = block.get("uri")
            parts.append("[attached resource: " + str(uri) + "]"
                         if isinstance(uri, str)
                         else "[attached resource: uri missing]")
        elif kind == "resource":
            parts.append(_embedded_text(block.get("resource")))
    return "\n\n".join(parts)


def _ok(msgid: JsonValue, result: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "id": msgid, "result": result}


def _fail(msgid: JsonValue, code: int, message: str) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "id": msgid,
            "error": {"code": code, "message": message}}


def _notify(method: str, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def _id_key(value: JsonValue) -> str:
    """Distinguish JSON-RPC ids by type as well as value: 1 and "1" differ."""
    if isinstance(value, bool):
        return "b:" + str(value)
    if isinstance(value, int):
        return "i:" + str(value)
    return "s:" + str(value)


def _pair_list(items: JsonValue) -> dict[str, str]:
    """[{name, value}] string pairs into a dict, dropping malformed rows."""
    out: dict[str, str] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if not is_json_object(item):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and name and isinstance(value, str):
            out[name] = value
    return out


def client_server_specs(servers: list) -> tuple[list, list]:
    """Translate ACP mcpServers into (accepted, skipped).

    Accepted entries are (name, config-dict) in the .mcp.json shape for
    mcp.set_client_servers. stdio entries need a command; http entries need a
    url. SSE entries are skipped: chad speaks stdio and streamable HTTP only.
    Names containing the tool separator, nameless entries and typeless
    entries are skipped with a reason each, never failing the session.
    """
    accepted: list = []
    skipped: list = []
    for entry in servers:
        if not is_json_object(entry):
            skipped.append("server entry is not an object; skipped")
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            skipped.append("server without a name; skipped")
            continue
        if "__" in name:
            skipped.append(name + ": name contains '__'; skipped")
            continue
        command = entry.get("command")
        url = entry.get("url")
        if isinstance(command, str) and command:
            raw_args = entry.get("args")
            args = [a for a in raw_args if isinstance(a, str)] \
                if isinstance(raw_args, list) else []
            accepted.append((name, {"command": command, "args": args,
                                    "env": _pair_list(entry.get("env"))}))
        elif isinstance(url, str) and url:
            if entry.get("type") == "sse":
                skipped.append(name + ": SSE transport is not supported; skipped")
                continue
            accepted.append((name, {"url": url,
                                    "headers": _pair_list(entry.get("headers"))}))
        else:
            skipped.append(name + ": neither command nor url; skipped")
    return accepted, skipped


def tool_title(name: str, args: dict) -> str:
    """One-line human title for a tool call, mirroring render_tool_start."""
    if name in ("edit", "write"):
        path = args.get("path")
        label = str(path) if isinstance(path, str) else "?"
        return ("Edit " if name == "edit" else "Write ") + label
    if name == "bash":
        command = args.get("command")
        flat = " ".join(str(command).split()) if isinstance(command, str) else ""
        return "Run " + (flat if len(flat) <= 60 else flat[:59] + "\u2026")
    if name == "write_todos":
        return "Plan"
    if name.startswith("mcp__"):
        bits = name[len("mcp__"):].split("__", 1)
        return "MCP " + (" / ".join(bits) if len(bits) == 2 else name)
    return name

def _json_scalar(value: JsonValue) -> JsonValue:
    """Coerce one JSON-ish value back into strict JSON (str fallback)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_scalar(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_scalar(v) for v in value]
    return str(value)


def _json_object(args: dict) -> dict[str, JsonValue]:
    """Copy tool args into strict JSON for rawInput (never raises)."""
    clean: dict[str, JsonValue] = {}
    for key, value in args.items():
        clean[str(key)] = _json_scalar(value)
    return clean


class AgentLike(Protocol):
    """The slice of Agent the ACP server drives (structural, for tests)."""

    mode: str
    interrupted: bool
    budget_note: str | None

    def run_turn(self, user_text: str, stream: bool = True) -> str: ...
    def save(self) -> None: ...


@dataclass
class _AcpSession:
    session_id: str
    cwd: str
    start_mode: str
    agent: Optional[AgentLike] = None
    cancel: threading.Event = field(default_factory=threading.Event)
    allow_always: set[str] = field(default_factory=set)
    tool_seq: int = 0
    current_tool: str | None = None
    mcp_notes: list = field(default_factory=list)
    mcp_wanted: bool = False


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: JsonValue = None
    error: str | None = None


class FakeAgent:
    """Scripted stand-in behind `chad acp --test-agent` and the unit tests.

    Drives the same closures a real Agent would (emit, confirm, tool_event,
    should_stop) so the ACP mapping is exercised without weights: echo only
    streams text, tool performs one bash call through the permission flow,
    and slow streams many chunks so cancellation has something to stop.
    """

    def __init__(self, cwd: str, mode: str, script: str, emit=None, confirm=None,
                 should_stop=None, tool_event=None):
        self.cwd = cwd
        self.mode = mode
        self.script = script
        self._emit = emit
        self._confirm = confirm
        self._should_stop = should_stop
        self._tool_event = tool_event
        self.interrupted = False
        self.budget_note: str | None = None
        self.saved = False

    def save(self) -> None:
        self.saved = True

    def run_turn(self, user_text: str, stream: bool = True) -> str:
        if self.script == "tool":
            return self._run_tool()
        if self.script == "slow":
            return self._run_slow()
        return self._run_echo(user_text)

    def _say(self, kind: str, text: str) -> None:
        if self._emit is not None:
            self._emit(kind, text)

    def _run_echo(self, user_text: str) -> str:
        self._say("stream", "You said: ")
        self._say("stream", user_text)
        return "Done."

    def _run_tool(self) -> str:
        args = {"command": "echo hi"}
        self._say("stream", "Working. ")
        self._tool_event("start", "bash", args)
        allowed = self._confirm("bash", args)
        if allowed:
            self._tool_event("finish", "bash", args, "[exit 0]\nhi\n")
            self._say("stream", "Finished. ")
            return "All done."
        self._tool_event("finish", "bash", args, "[denied by user]")
        return "The command was denied."

    def _run_slow(self) -> str:
        for i in range(40):
            if self._should_stop is not None and self._should_stop():
                self.interrupted = True
                return "[interrupted]"
            self._say("stream", "chunk" + str(i) + " ")
            time.sleep(0.02)
        return "Slow done."


def fake_agent_factory(script: str):
    """Build a make_agent closure serving FakeAgents (tests and --test-agent)."""
    if script not in ("echo", "tool", "slow"):
        raise ValueError("unknown test script: " + script)

    def make_agent(cwd, mode, emit, confirm, should_stop, tool_event):
        return FakeAgent(cwd, mode, script, emit=emit, confirm=confirm,
                         should_stop=should_stop, tool_event=tool_event)

    return make_agent


class AcpServer:
    """One ACP v1 agent endpoint over injected line streams.

    readline returns the next client line (None on EOF); writeline takes one
    outgoing line. make_agent(cwd, mode, emit, confirm, should_stop,
    tool_event) builds the turn runner for a session, lazily on first prompt
    so session/new never disturbs a running turn. Prompts execute on worker
    threads under one engine lock; the reader keeps serving cancel.
    """

    def __init__(self, readline: Callable[[], Optional[str]],
                 writeline: Callable[[str], None], make_agent,
                 default_mode: str = "normal"):
        self._readline = readline
        self._writeline = writeline
        self._make_agent = make_agent
        self._default_mode = default_mode
        self._send_lock = threading.Lock()
        self._sessions: dict[str, _AcpSession] = {}
        self._sessions_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._request_seq = 0
        self._prompt_lock = threading.Lock()
        self._prompt_requests: dict[str, str] = {}
        self._prompt_requests_lock = threading.Lock()
        self._running = True

    def serve_forever(self) -> None:
        """Read requests and notifications until EOF."""
        while self._running:
            try:
                line = self._readline()
            except Exception:  # noqa: BLE001 - a dead stdin ends the agent
                break
            if line is None:
                break
            self.handle_line(line)

    def handle_line(self, line: str) -> None:
        """Parse and dispatch one NDJSON line (tests drive the server here)."""
        text = line.strip()
        if not text:
            return
        try:
            message = json.loads(text)
        except ValueError:
            self._send({"jsonrpc": "2.0", "id": None,
                        "error": {"code": _PARSE_ERROR, "message": "invalid JSON"}})
            return
        if not is_json_object(message):
            self._send({"jsonrpc": "2.0", "id": None,
                        "error": {"code": _INVALID_REQUEST,
                                 "message": "message is not an object"}})
            return
        self._handle_object(message)

    def _send(self, message: dict[str, JsonValue]) -> None:
        line = json.dumps(message)
        with self._send_lock:
            self._writeline(line)

    def _handle_object(self, message: dict[str, JsonValue]) -> None:
        method = message.get("method")
        if isinstance(method, str):
            params = message.get("params")
            parsed = params if is_json_object(params) else {}
            if "id" in message:
                self._dispatch_request(message["id"], method, parsed)
            else:
                self._dispatch_notification(method, parsed)
            return
        if "id" in message:
            self._dispatch_response(message["id"], message)
        # No method and no id: nothing to answer; drop it rather than guessing.

    def _dispatch_request(self, msgid: JsonValue, method: str,
                          params: dict[str, JsonValue]) -> None:
        try:
            if method == "session/prompt":
                self._on_prompt(msgid, params)
                return
            result = self._on_request(method, params)
        except _RequestError as e:
            self._send(_fail(msgid, e.code, e.message))
            return
        except Exception as e:  # noqa: BLE001 - a handler fault must not kill us
            sys.stderr.write("chad acp: " + method + " failed: " + repr(e) + "\n")
            self._send(_fail(msgid, _INTERNAL_ERROR, "internal error"))
            return
        self._send(_ok(msgid, result))

    def _on_request(self, method: str,
                    params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if method == "initialize":
            return self._on_initialize(params)
        if method == "authenticate":
            return {}
        if method == "session/new":
            return self._on_new_session(params)
        if method == "session/set_mode":
            return self._on_set_mode(params)
        raise _RequestError(_METHOD_NOT_FOUND, "unknown method: " + method)

    def _on_initialize(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {"protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": False,
                                      "promptCapabilities": {"embeddedContext": True}}}

    def _session_or_error(self, params: dict[str, JsonValue]) -> _AcpSession:
        session_id = _str_field(params, "sessionId")
        if session_id is None:
            raise _RequestError(_INVALID_PARAMS, "missing sessionId")
        with self._sessions_lock:
            rec = self._sessions.get(session_id)
        if rec is None:
            raise _RequestError(_INVALID_PARAMS, "unknown session")
        return rec

    def _on_new_session(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        cwd = _str_field(params, "cwd")
        if not cwd or not os.path.isdir(cwd):
            raise _RequestError(_INVALID_PARAMS, "session/new needs an existing cwd")
        session_id = session_store.new_session_id()
        rec = _AcpSession(session_id=session_id, cwd=cwd,
                          start_mode=self._default_mode)
        raw_servers = params.get("mcpServers")
        accepted, skipped = client_server_specs(
            raw_servers if isinstance(raw_servers, list) else [])
        rec.mcp_wanted = "mcpServers" in params
        rec.mcp_notes = skipped + mcp.set_client_servers(cwd, accepted)
        with self._sessions_lock:
            self._sessions[session_id] = rec
        available: list = []
        for mid, mname, mdesc in _MODES:
            available.append({"id": mid, "name": mname, "description": mdesc})
        return {"sessionId": session_id,
                "modes": {"currentModeId": rec.start_mode,
                          "availableModes": available}}

    def _on_set_mode(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        rec = self._session_or_error(params)
        mode = _str_field(params, "modeId")
        if mode not in _MODE_IDS:
            raise _RequestError(_INVALID_PARAMS, "unknown mode")
        rec.start_mode = mode
        if rec.agent is not None:
            rec.agent.mode = mode
        return {}

    def _dispatch_notification(self, method: str,
                               params: dict[str, JsonValue]) -> None:
        if method == "session/cancel":
            session_id = _str_field(params, "sessionId")
            if session_id is None:
                return
            with self._sessions_lock:
                rec = self._sessions.get(session_id)
            if rec is not None:
                rec.cancel.set()
            return
        if method == "$/cancel_request":
            raw = params.get("id")
            if raw is None:
                return
            with self._prompt_requests_lock:
                session_id = self._prompt_requests.get(_id_key(raw))
            if session_id is None:
                return
            with self._sessions_lock:
                rec = self._sessions.get(session_id)
            if rec is not None:
                rec.cancel.set()
        # Unknown notifications are ignored: a newer client may chatter.

    def _dispatch_response(self, msgid: JsonValue,
                           message: dict[str, JsonValue]) -> None:
        if isinstance(msgid, bool) or not isinstance(msgid, int):
            return  # only our own integer ids can match a pending request
        with self._pending_lock:
            pending = self._pending.get(msgid)
        if pending is None:
            return  # late answer to an abandoned wait; nothing to do
        error = message.get("error")
        if is_json_object(error):
            detail = error.get("message")
            pending.error = str(detail) if isinstance(detail, str) else "client error"
        elif "result" in message:
            pending.result = message["result"]
        else:
            pending.error = "empty response"
        pending.event.set()

    def _on_prompt(self, msgid: JsonValue, params: dict[str, JsonValue]) -> None:
        rec = self._session_or_error(params)
        prompt = params.get("prompt")
        if not isinstance(prompt, list):
            raise _RequestError(_INVALID_PARAMS, "session/prompt needs prompt blocks")
        if not self._prompt_lock.acquire(blocking=False):
            raise _RequestError(_INTERNAL_ERROR, "busy: another prompt is running")
        key = _id_key(msgid)
        with self._prompt_requests_lock:
            self._prompt_requests[key] = rec.session_id
        worker = threading.Thread(target=self._run_prompt,
                                  args=(msgid, key, rec, prompt), daemon=True)
        worker.start()

    def _run_prompt(self, msgid: JsonValue, key: str, rec: _AcpSession,
                    blocks: list) -> None:
        try:
            text = prompt_text(blocks)
            if not text.strip():
                raise _RequestError(_INVALID_PARAMS, "prompt has no text")
            rec.cancel.clear()
            try:
                previous_cwd = os.getcwd()
                os.chdir(rec.cwd)
            except OSError as e:
                raise _RequestError(_INVALID_PARAMS,
                                    "cannot enter session cwd: " + str(e))
            try:
                # Build the Agent inside the session cwd: the system prompt,
                # skills, MCP servers and the session store all read the
                # process cwd at construction, so building it in the repo
                # checkout would aim the model at the wrong tree.
                agent = rec.agent
                if agent is None:
                    try:
                        agent = self._make_agent(rec.cwd, rec.start_mode,
                                                 self._emit_closure(rec),
                                                 self._confirm_closure(rec),
                                                 rec.cancel.is_set,
                                                 self._tool_event_closure(rec))
                    except SystemExit:
                        raise _RequestError(_INTERNAL_ERROR, "agent startup aborted")
                    rec.agent = agent
                if rec.mcp_wanted:
                    self._announce_mcp(rec)
                final = agent.run_turn(text)
                try:
                    agent.save()
                except Exception as e:  # noqa: BLE001 - a save fault is not a turn fault
                    sys.stderr.write("chad acp: save failed: " + repr(e) + "\n")
            finally:
                os.chdir(previous_cwd)
            if rec.cancel.is_set() or agent.interrupted:
                self._send(_ok(msgid, {"stopReason": "cancelled"}))
                return
            stop = "max_turn_requests" if agent.budget_note else "end_turn"
            if final and final.strip():
                self._message(rec, final)
            self._send(_ok(msgid, {"stopReason": stop}))
        except _RequestError as e:
            self._send(_fail(msgid, e.code, e.message))
        except SystemExit:  # noqa: BLE001 - startup helpers exit; answer instead
            self._send(_fail(msgid, _INTERNAL_ERROR, "agent startup aborted"))
        except Exception as e:  # noqa: BLE001 - every prompt deserves an answer
            sys.stderr.write("chad acp: prompt failed: " + repr(e) + "\n")
            self._send(_fail(msgid, _INTERNAL_ERROR, "internal error"))
        finally:
            with self._prompt_requests_lock:
                self._prompt_requests.pop(key, None)
            self._prompt_lock.release()

    def _emit_closure(self, rec: _AcpSession):
        def emit(kind: str, text: str) -> None:
            self._emit(rec, kind, text)
        return emit

    def _confirm_closure(self, rec: _AcpSession):
        def confirm(name: str, args: dict) -> bool:
            return self._confirm(rec, name, args)
        return confirm

    def _tool_event_closure(self, rec: _AcpSession):
        def tool_event(phase: str, name: str, args: dict,
                       result: str | None = None) -> None:
            self._tool_event(rec, phase, name, args, result)
        return tool_event


    def _announce_mcp(self, rec: _AcpSession) -> None:
        # One info line on the session MCP servers (best-effort).
        try:
            rows = [ln for ln in mcp.summary_lines() if not ln.startswith(" ")]
        except Exception as e:  # noqa: BLE001 - MCP must never break a prompt
            self._message(rec, "MCP unavailable: " + repr(e))
            return
        rows = [ln for ln in rows if not ln.startswith("no MCP servers configured")]
        notes = list(rec.mcp_notes)
        if not rows and not notes:
            return
        body = "MCP: " + ("; ".join(rows[:6]) if rows else "no servers connected")
        if notes:
            body += " | client servers: " + "; ".join(notes[:4])
        try:
            self._message(rec, body[:1200])
        except Exception:  # noqa: BLE001 - telemetry must never break a turn
            pass

    def _message(self, rec: _AcpSession, text: str) -> None:
        self._send(_notify("session/update",
                           {"sessionId": rec.session_id,
                            "update": {"sessionUpdate": "agent_message_chunk",
                                       "content": {"type": "text", "text": text}}}))

    def _emit(self, rec: _AcpSession, kind: str, text: str) -> None:
        """Forward one Agent emit as a session/update (best-effort)."""
        try:
            if kind == "stream" or kind == "info":
                self._message(rec, text)
            elif kind == "think":
                self._send(_notify(
                    "session/update",
                    {"sessionId": rec.session_id,
                     "update": {"sessionUpdate": "agent_thought_chunk",
                                "content": {"type": "text", "text": text}}}))
            # Terminal-only kinds (tool headers, diff lines, gauges) stay out:
            # the structured tool_call updates below carry that content.
        except Exception:  # noqa: BLE001 - telemetry must never break a turn
            pass

    def _locations(self, name: str, args: dict) -> list:
        if name not in ("edit", "write"):
            return []
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return []
        try:
            return [{"path": os.path.abspath(path)}]
        except Exception:  # noqa: BLE001 - locations are decorative
            return []

    def _announce(self, rec: _AcpSession, name: str, args: dict) -> str:
        """Send the tool_call notification; the id stays current until finish."""
        rec.tool_seq += 1
        tool_id = "call-" + str(rec.tool_seq)
        rec.current_tool = tool_id
        self._send(_notify(
            "session/update",
            {"sessionId": rec.session_id,
             "update": {"sessionUpdate": "tool_call",
                        "toolCallId": tool_id,
                        "title": tool_title(name, args),
                        "kind": _TOOL_KINDS.get(name, "other"),
                        "status": "pending",
                        "locations": self._locations(name, args),
                        "rawInput": _json_object(args)}}))
        return tool_id

    def _update_tool(self, rec: _AcpSession, tool_id: str, status: str,
                     content: list) -> None:
        self._send(_notify(
            "session/update",
            {"sessionId": rec.session_id,
             "update": {"sessionUpdate": "tool_call_update",
                        "toolCallId": tool_id,
                        "status": status,
                        "content": content}}))

    def _finish_content(self, name: str, args: dict,
                        result: str | None) -> list:
        text = result if isinstance(result, str) else ""
        clipped = text if len(text) <= _RESULT_CLIP else text[:_RESULT_CLIP] + "\n... (truncated)"
        if name in ("edit", "write"):
            path = args.get("path")
            if isinstance(path, str) and path:
                try:
                    shown = os.path.abspath(path)
                except Exception:  # noqa: BLE001 - fall back to the raw path
                    shown = path
                if name == "edit":
                    old = args.get("old")
                    new = args.get("new")
                    diff: dict[str, JsonValue] = {
                        "type": "diff", "path": shown,
                        "newText": str(new)[:_DIFF_CLIP] if isinstance(new, str) else ""}
                    if isinstance(old, str):
                        diff["oldText"] = old[:_DIFF_CLIP]
                else:
                    body = args.get("content")
                    diff = {"type": "diff", "path": shown,
                            "newText": str(body)[:_DIFF_CLIP]
                            if isinstance(body, str) else ""}
                out: list = [diff]
                if clipped:
                    out.append({"type": "content",
                                "content": {"type": "text", "text": clipped}})
                return out
        return [{"type": "content", "content": {"type": "text", "text": clipped}}]

    def _tool_event(self, rec: _AcpSession, phase: str, name: str, args: dict,
                    result: str | None = None) -> None:
        """Structured tool boundary from Agent (start around dispatch, finish after)."""
        try:
            if phase == "start":
                if rec.current_tool is None:
                    self._announce(rec, name, args)  # auto-approved: no confirm ran
                return
            if phase != "finish":
                return
            tool_id = rec.current_tool
            rec.current_tool = None
            if tool_id is None:
                return
            failed = isinstance(result, str) and result.startswith(_FINISH_FAIL_PREFIXES)
            self._update_tool(rec, tool_id, "failed" if failed else "completed",
                              self._finish_content(name, args, result))
        except Exception:  # noqa: BLE001 - telemetry must never break a turn
            pass

    def _confirm(self, rec: _AcpSession, name: str, args: dict) -> bool:
        """Ask the client; an allow-always answer persists for the session."""
        if name in rec.allow_always:
            return True
        tool_id = rec.current_tool
        if tool_id is None:
            tool_id = self._announce(rec, name, args)
        snapshot: dict[str, JsonValue] = {
            "toolCallId": tool_id,
            "title": tool_title(name, args),
            "kind": _TOOL_KINDS.get(name, "other"),
            "status": "pending",
            "rawInput": _json_object(args)}
        request_id = self._client_request(
            "session/request_permission",
            {"sessionId": rec.session_id,
             "toolCall": snapshot,
             "options": [{"optionId": "allow-once", "name": "Allow once",
                          "kind": "allow_once"},
                         {"optionId": "allow-always", "name": "Allow always",
                          "kind": "allow_always"},
                         {"optionId": "reject-once", "name": "Reject",
                          "kind": "reject_once"}]})
        answer = self._await_permission(request_id, rec)
        if answer == "allow-once":
            return True
        if answer == "allow-always":
            rec.allow_always.add(name)
            return True
        self._update_tool(rec, tool_id, "failed",
                          [{"type": "content",
                            "content": {"type": "text", "text": "Denied by user."}}])
        rec.current_tool = None
        return False

    def _client_request(self, method: str,
                        params: dict[str, JsonValue]) -> int:
        """Send a request to the client; the caller waits on the returned id."""
        with self._pending_lock:
            self._request_seq += 1
            request_id = self._request_seq
            self._pending[request_id] = _Pending()
        self._send({"jsonrpc": "2.0", "id": request_id,
                    "method": method, "params": params})
        return request_id

    def _await_permission(self, request_id: int, rec: _AcpSession) -> str:
        """Wait for the permission answer; cancel or client errors deny."""
        with self._pending_lock:
            pending = self._pending.get(request_id)
        while pending is not None and self._running:
            if rec.cancel.is_set():
                break
            if pending.event.wait(0.05):
                break
        with self._pending_lock:
            self._pending.pop(request_id, None)
        if pending is None or rec.cancel.is_set() or not self._running:
            return "cancelled"
        if pending.error is not None:
            sys.stderr.write("chad acp: permission request failed: " +
                             pending.error + "\n")
            return "reject-once"
        result = pending.result
        if is_json_object(result):
            outcome = result.get("outcome")
            if is_json_object(outcome):
                if outcome.get("outcome") == "cancelled":
                    return "cancelled"
                if outcome.get("outcome") == "selected":
                    option = outcome.get("optionId")
                    if option in ("allow-once", "allow-always", "reject-once"):
                        return str(option)
        return "reject-once"  # unknown shape: deny, the safe default


class _LazyEngine:
    """Build the MLX engine once, on first prompt (download happens here)."""

    def __init__(self, model_id: str, thinking: bool):
        self._model_id = model_id
        self._thinking = thinking
        self._lock = threading.Lock()
        self._bundle = None

    def bundle(self):
        """The shared (engine, ctx_limit) pair, built once under a lock."""
        if self._bundle is not None:
            return self._bundle
        with self._lock:
            if self._bundle is not None:
                return self._bundle
            from . import cli as _cli
            from . import config as _config
            backend = _cli._load_backend()
            _cli._ensure_model(self._model_id)
            cache_dir = os.path.expanduser("~/.cache/chad/kv")
            cache_gb = _config.env_int("CHAD_KV_CACHE_MAX_GB")
            engine = backend.engine(
                model_id=self._model_id,
                kv_bits=_config.env_int("CHAD_KV_BITS"),
                max_context=_config.env_int("CHAD_MAX_CONTEXT"),
                cache_dir=cache_dir,
                kv_cache_max_bytes=(cache_gb if cache_gb is not None else 8)
                * 1024 ** 3)
            _cli.apply_sampler_preset(engine, self._thinking)
            _cli.apply_sampler_env(engine)
            engine.load()
            self._bundle = (engine, _cli._compute_ctx_limit(engine))
            return self._bundle


def _stdin_line() -> Optional[str]:
    raw = sys.stdin.buffer.readline()
    if not raw:
        return None
    return raw.decode("utf-8", "replace")


def _stdout_line(line: str) -> None:
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def run(args, host=None) -> int:
    """`chad acp` entrypoint: serve ACP v1 on stdio until EOF."""
    from . import cli as _cli
    from .agent import Agent
    if host is None:
        host = _cli.HOST
    default_mode = "plan" if args.plan else ("yolo" if args.yolo else "normal")
    thinking = not args.no_think
    if args.test_agent is not None:
        make_agent = fake_agent_factory(args.test_agent)
    else:
        _cli._preflight("mlx", host=host)
        model_id, _why = _cli._pick_model(args.model, host=host)
        engines = _LazyEngine(model_id, thinking)

        def make_agent(cwd, mode, emit, confirm, should_stop, tool_event):
            engine, ctx_limit = engines.bundle()
            return Agent(engine, yolo=(mode == "yolo"), mode=mode,
                         thinking=thinking, emit=emit, confirm=confirm,
                         should_stop=should_stop, tool_event=tool_event,
                         ctx_limit=ctx_limit, persist=True)

    server = AcpServer(_stdin_line, _stdout_line, make_agent,
                       default_mode=default_mode)
    sys.stderr.write("chad acp: speaking ACP v1 over stdio " +
                     "(protocol traffic on stdout only)\n")
    server.serve_forever()
    return 0
