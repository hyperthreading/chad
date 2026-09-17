"""ACP adapter tests: protocol framing plus the Agent mapping, model-free.

A queue-backed Rig drives a real AcpServer the way Zed would: NDJSON lines in,
NDJSON lines out, with FakeAgents (see chad.acp) standing in for weights. The
official TypeScript client harness in tests/acp_client covers the same ground
against the real stdio transport; these tests pin the contract in the gate.
"""
import json
import os
import queue
import sys
import threading
import time

from chad import acp, mcp
from chad.agent import Agent
from chad.base_engine import GenStats


class Rig:
    """In-process JSON-RPC client: NDJSON lines across two queues."""

    def __init__(self, script, default_mode="normal",
                 connect_client_servers=True):
        self.incoming = queue.Queue()
        self.outgoing = queue.Queue()
        self.created = []
        inner = acp.fake_agent_factory(script)

        def recording_factory(cwd, mode, emit, confirm, should_stop, tool_event,
                              session_id=None):
            agent = inner(cwd, mode, emit, confirm, should_stop, tool_event)
            self.created.append(agent)
            return agent

        self.server = acp.AcpServer(
            self._readline, self._writeline, recording_factory,
            default_mode=default_mode,
            connect_client_servers=connect_client_servers)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self._seq = 0
        self.thread.start()

    def _readline(self):
        line = self.incoming.get()
        return None if line is None else line

    def _writeline(self, line):
        self.outgoing.put(line)

    def close(self):
        self.server._running = False
        self.incoming.put(None)
        self.thread.join(timeout=10)

    def send(self, obj):
        self.incoming.put(json.dumps(obj))

    def request(self, method, params):
        self._seq += 1
        self.send({"jsonrpc": "2.0", "id": self._seq,
                   "method": method, "params": params})
        return self._seq

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def next_message(self, timeout=10.0):
        return json.loads(self.outgoing.get(timeout=timeout))

    def expect_response(self, rid, timeout=10.0):
        """Collect notifications until the response with id rid arrives."""
        notes = []
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            assert remaining > 0, "timed out waiting for response"
            msg = json.loads(self.outgoing.get(timeout=remaining))
            if "method" in msg:
                notes.append(msg)
                continue
            assert msg.get("id") == rid, "wrong response id: %r" % (msg,)
            return msg, notes

    def initialize(self):
        rid = self.request("initialize", {"protocolVersion": 1})
        return self.expect_response(rid)

    def new_session(self, cwd):
        rid = self.request("session/new", {"cwd": str(cwd), "mcpServers": []})
        return self.expect_response(rid)


def _updates(notes, kind):
    out = []
    for note in notes:
        if note.get("method") != "session/update":
            continue
        update = note["params"]["update"]
        if update.get("sessionUpdate") == kind:
            out.append(update)
    return out


def test_initialize_returns_v1():
    rig = Rig("echo")
    try:
        msg, _ = rig.initialize()
        assert msg["result"]["protocolVersion"] == 1
        caps = msg["result"]["agentCapabilities"]
        assert caps["loadSession"] is False
        assert caps["promptCapabilities"]["embeddedContext"] is True
    finally:
        rig.close()


def test_new_session_validates_cwd(tmp_path):
    rig = Rig("echo")
    try:
        rid = rig.request("session/new", {"cwd": "/nonexistent-dir-xyz",
                                          "mcpServers": []})
        msg, _ = rig.expect_response(rid)
        assert msg["error"]["code"] == -32602
        msg, _ = rig.new_session(tmp_path)
        assert isinstance(msg["result"]["sessionId"], str)
        assert msg["result"]["modes"]["currentModeId"] == "normal"
        ids = [m["id"] for m in msg["result"]["modes"]["availableModes"]]
        assert ids == ["normal", "auto", "yolo", "plan"]
    finally:
        rig.close()


def test_echo_roundtrip_restores_cwd(tmp_path):
    rig = Rig("echo")
    try:
        rig.initialize()
        msg, _ = rig.new_session(tmp_path)
        session_id = msg["result"]["sessionId"]
        before = os.getcwd()
        rid = rig.request("session/prompt",
                          {"sessionId": session_id,
                           "prompt": [{"type": "text", "text": "hello"}]})
        msg, notes = rig.expect_response(rid)
        assert msg["result"]["stopReason"] == "end_turn"
        chunks = _updates(notes, "agent_message_chunk")
        assert any("hello" in c["content"]["text"] for c in chunks)
        assert any("Done." in c["content"]["text"] for c in chunks)
        assert os.getcwd() == before
        assert rig.created[0].saved is True
    finally:
        rig.close()


def _run_tool_prompt(rig, session_id, answer, timeout=10.0):
    """Drive one tool-script prompt; answer the permission request.

    Returns (prompt_response, notes, permission_request_or_None).
    """
    rid = rig.request("session/prompt",
                      {"sessionId": session_id,
                       "prompt": [{"type": "text", "text": "go"}]})
    notes = []
    perm = None
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        assert remaining > 0, "timed out waiting for prompt answer"
        msg = rig.next_message(timeout=remaining)
        if msg.get("method") == "session/request_permission":
            perm = msg
            choice = {"optionId": answer, "outcome": "selected"}
            rig.send({"jsonrpc": "2.0", "id": msg["id"],
                      "result": {"outcome": choice}})
            continue
        if "method" in msg:
            notes.append(msg)
            continue
        assert msg.get("id") == rid, "wrong response id: %r" % (msg,)
        return msg, notes, perm


def test_tool_permission_approve(tmp_path):
    rig = Rig("tool")
    try:
        rig.initialize()
        msg, _ = rig.new_session(tmp_path)
        session_id = msg["result"]["sessionId"]
        msg, notes, perm = _run_tool_prompt(rig, session_id, "allow-once")
        assert perm is not None
        assert perm["params"]["toolCall"]["title"] == "Run echo hi"
        assert perm["params"]["toolCall"]["kind"] == "execute"
        calls = _updates(notes, "tool_call")
        assert len(calls) == 1 and calls[0]["toolCallId"] == "call-1"
        updates = _updates(notes, "tool_call_update")
        assert len(updates) == 1
        assert updates[0]["status"] == "completed"
        assert updates[0]["toolCallId"] == "call-1"
        assert msg["result"]["stopReason"] == "end_turn"
        chunks = _updates(notes, "agent_message_chunk")
        assert any("All done." in c["content"]["text"] for c in chunks)
    finally:
        rig.close()


def test_tool_permission_reject(tmp_path):
    rig = Rig("tool")
    try:
        rig.initialize()
        msg, _ = rig.new_session(tmp_path)
        session_id = msg["result"]["sessionId"]
        msg, notes, perm = _run_tool_prompt(rig, session_id, "reject-once")
        assert perm is not None
        updates = _updates(notes, "tool_call_update")
        assert len(updates) == 1
        assert updates[0]["status"] == "failed"
        assert msg["result"]["stopReason"] == "end_turn"
        chunks = _updates(notes, "agent_message_chunk")
        assert any("denied" in c["content"]["text"] for c in chunks)
    finally:
        rig.close()


def test_allow_always_skips_second_ask(tmp_path):
    rig = Rig("tool")
    try:
        rig.initialize()
        msg, _ = rig.new_session(tmp_path)
        session_id = msg["result"]["sessionId"]
        msg, _, perm = _run_tool_prompt(rig, session_id, "allow-always")
        assert perm is not None
        assert msg["result"]["stopReason"] == "end_turn"
        msg, notes, perm = _run_tool_prompt(rig, session_id, "reject-once")
        assert perm is None  # remembered: no second question
        assert msg["result"]["stopReason"] == "end_turn"
        updates = _updates(notes, "tool_call_update")
        assert updates and updates[0]["status"] == "completed"
    finally:
        rig.close()


def test_cancel_slow_prompt(tmp_path):
    rig = Rig("slow")
    try:
        rig.initialize()
        msg, _ = rig.new_session(tmp_path)
        session_id = msg["result"]["sessionId"]
        rid = rig.request("session/prompt",
                          {"sessionId": session_id,
                           "prompt": [{"type": "text", "text": "take your time"}]})
        time.sleep(0.3)
        rig.notify("session/cancel", {"sessionId": session_id})
        msg, notes = rig.expect_response(rid)
        assert msg["result"]["stopReason"] == "cancelled"
        assert _updates(notes, "agent_message_chunk")
    finally:
        rig.close()


def test_second_prompt_while_busy(tmp_path):
    rig = Rig("slow")
    try:
        rig.initialize()
        msg, _ = rig.new_session(tmp_path)
        session_id = msg["result"]["sessionId"]
        rid1 = rig.request("session/prompt",
                           {"sessionId": session_id,
                            "prompt": [{"type": "text", "text": "one"}]})
        time.sleep(0.1)
        rid2 = rig.request("session/prompt",
                           {"sessionId": session_id,
                            "prompt": [{"type": "text", "text": "two"}]})
        msg2, _ = rig.expect_response(rid2)
        assert msg2["error"]["code"] == -32603
        rig.notify("session/cancel", {"sessionId": session_id})
        msg1, _ = rig.expect_response(rid1)
        assert msg1["result"]["stopReason"] == "cancelled"
    finally:
        rig.close()


def test_cancel_during_permission(tmp_path):
    rig = Rig("tool")
    try:
        rig.initialize()
        msg, _ = rig.new_session(tmp_path)
        session_id = msg["result"]["sessionId"]
        rid = rig.request("session/prompt",
                          {"sessionId": session_id,
                           "prompt": [{"type": "text", "text": "go"}]})
        deadline = time.time() + 10.0
        notes = []
        while True:
            msg = rig.next_message(timeout=max(0.1, deadline - time.time()))
            if msg.get("method") == "session/request_permission":
                break
            if "method" in msg:
                notes.append(msg)
        rig.notify("session/cancel", {"sessionId": session_id})
        msg, rest = rig.expect_response(rid)
        assert msg["result"]["stopReason"] == "cancelled"
        failed = _updates(notes + rest, "tool_call_update")
        assert failed and failed[0]["status"] == "failed"
    finally:
        rig.close()


def test_unknown_method_and_bad_params(tmp_path):
    rig = Rig("echo")
    try:
        rid = rig.request("session/nope", {})
        msg, _ = rig.expect_response(rid)
        assert msg["error"]["code"] == -32601
        rid = rig.request("session/prompt",
                          {"sessionId": "missing",
                           "prompt": [{"type": "text", "text": "hi"}]})
        msg, _ = rig.expect_response(rid)
        assert msg["error"]["code"] == -32602
        rid = rig.request("session/prompt", {"sessionId": "missing"})
        msg, _ = rig.expect_response(rid)
        assert msg["error"]["code"] == -32602
    finally:
        rig.close()


def test_set_mode_reaches_agent(tmp_path):
    rig = Rig("echo")
    try:
        rig.initialize()
        msg, _ = rig.new_session(tmp_path)
        session_id = msg["result"]["sessionId"]
        rid = rig.request("session/set_mode",
                          {"sessionId": session_id, "modeId": "bogus"})
        msg, _ = rig.expect_response(rid)
        assert msg["error"]["code"] == -32602
        rid = rig.request("session/set_mode",
                          {"sessionId": session_id, "modeId": "yolo"})
        msg, _ = rig.expect_response(rid)
        assert msg["result"] == {}
        rid = rig.request("session/prompt",
                          {"sessionId": session_id,
                           "prompt": [{"type": "text", "text": "hi"}]})
        msg, _ = rig.expect_response(rid)
        assert msg["result"]["stopReason"] == "end_turn"
        assert rig.created[0].mode == "yolo"
    finally:
        rig.close()


def test_prompt_text_blocks():
    blocks = [
        {"type": "text", "text": "fix it"},
        {"type": "image", "data": "x", "mimeType": "image/png"},
        {"type": "audio", "data": "y", "mimeType": "audio/wav"},
        {"type": "resource_link", "name": "a", "uri": "file:///x.py"},
        {"type": "resource",
         "resource": {"uri": "file:///b.py", "text": "print(1)\n"}},
        {"type": "resource", "resource": {"uri": "file:///c.bin"}},
        {"type": "future", "text": "skip me"},
        "not-an-object",
    ]
    text = acp.prompt_text(blocks)
    assert "fix it" in text
    assert "text-only" in text
    assert "file:///x.py" in text
    assert "print(1)" in text
    assert "binary" in text
    assert "skip me" not in text


def test_tool_title_shapes():
    assert acp.tool_title("edit", {"path": "a.py"}) == "Edit a.py"
    assert acp.tool_title("write", {"path": "b.py"}) == "Write b.py"
    assert acp.tool_title("bash", {"command": "pytest -q"}) == "Run pytest -q"
    assert acp.tool_title("write_todos", {}) == "Plan"
    assert acp.tool_title("mcp__srv__tool", {}) == "MCP srv / tool"
    assert acp.tool_title("done", {}) == "done"


def test_malformed_line_answers_parse_error():
    rig = Rig("echo")
    try:
        rig.incoming.put("this is not json")
        msg = rig.next_message()
        assert msg["error"]["code"] == -32700
        assert msg["id"] is None
    finally:
        rig.close()


def test_real_agent_fires_tool_event(tmp_path, monkeypatch):
    """The Agent hook acp.py relies on: start/finish around a real dispatch."""
    import json as _json

    from chad.agent import Agent
    from chad.base_engine import GenStats

    class _Tok:
        def apply_chat_template(self, messages, tools=None,
                                add_generation_prompt=False, enable_thinking=False):
            return list(range(sum(len(m.get("content", "")) for m in messages) // 4 + 8))

        def decode(self, ids, skip_special_tokens=False):
            return ""

    def _call(name, **args):
        return "<tool_call>\n" + _json.dumps({"name": name, "arguments": args}) + "\n</tool_call>"

    class _Scripted:
        def __init__(self, script):
            self.script = list(script)
            self.model_id = "scripted"
            self.effective_ctx = 24000
            self.cache_dir = None
            self._cached_ids = []
            self.tok = _Tok()

        def generate(self, prompt_ids, max_tokens=2048, on_token=None,
                     stop_texts=None, should_stop=None, on_prefill=None,
                     on_prefill_progress=None, stop_condition=None,
                     think_ceiling=None):
            text = self.script.pop(0)
            if on_token:
                on_token(text)
            return text, GenStats(prompt_tokens=len(prompt_ids))

        def reset(self):
            self._cached_ids = []

        def warm_prefix(self, prefix_ids, should_stop=None, head_ids=None):
            return "skip", 0

    monkeypatch.chdir(tmp_path)
    target = tmp_path / "note.txt"
    events = []
    agent = Agent(
        _Scripted([_call("write", path=str(target), content="hi\n"),
                   _call("bash", command="cat " + str(target)),
                   _call("done", summary="ok")]),
        mode="yolo", thinking=False,
        tool_event=lambda *parts: events.append(parts))
    agent.run_turn("create the note")
    starts = [e for e in events if e[0] == "start"]
    finishes = [e for e in events if e[0] == "finish"]
    assert [e[1] for e in starts] == ["write", "bash"]
    assert [e[1] for e in finishes] == ["write", "bash"]
    assert starts[0][2]["path"] == str(target)
    assert finishes[0][3].startswith("[wrote")
    assert "hi" in finishes[1][3]
    assert target.read_text() == "hi\n"


def test_client_server_translation():
    accepted, skipped = acp.client_server_specs([
        {"name": "a", "command": "srv", "args": ["x", 7],
         "env": [{"name": "K", "value": "v"}, {"name": "bad"}]},
        {"name": "b", "type": "http", "url": "https://h",
         "headers": [{"name": "Auth", "value": "t"}]},
        {"name": "c", "type": "sse", "url": "https://s"},
        {"name": "x__y", "command": "srv"},
        {"command": "nameless"},
        {"name": "d"},
        "junk",
    ])
    assert [n for n, _s in accepted] == ["a", "b"]
    assert accepted[0][1] == {"command": "srv", "args": ["x"], "env": {"K": "v"}}
    assert accepted[1][1] == {"url": "https://h", "headers": {"Auth": "t"}}
    assert len(skipped) == 5
    assert any("SSE" in s for s in skipped)
    assert any("__" in s for s in skipped)


_MCP_STUB = (
    "import json, sys\n"
    "TOOLS = [{\"name\": \"echo\", \"description\": \"Echo.\", "
    "\"inputSchema\": {\"type\": \"object\", \"properties\": {\"text\": {\"type\": \"string\"}}}, "
    "\"annotations\": {\"readOnlyHint\": True}}, "
    "{\"name\": \"do\", \"description\": \"Do.\", "
    "\"inputSchema\": {\"type\": \"object\", \"properties\": {\"text\": {\"type\": \"string\"}}}}]\n"
    "def send(m):\n"
    "    sys.stdout.write(json.dumps(m) + \"\\n\")\n"
    "    sys.stdout.flush()\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if not line:\n"
    "        continue\n"
    "    req = json.loads(line)\n"
    "    mid = req.get(\"id\")\n"
    "    method = req.get(\"method\")\n"
    "    if method == \"initialize\":\n"
    "        send({\"jsonrpc\": \"2.0\", \"id\": mid, \"result\": {\"protocolVersion\": req[\"params\"][\"protocolVersion\"], \"capabilities\": {\"tools\": {}}, \"serverInfo\": {\"name\": \"stub\", \"version\": \"1.0\"}}})\n"
    "    elif method == \"notifications/initialized\":\n"
    "        pass\n"
    "    elif method == \"tools/list\":\n"
    "        send({\"jsonrpc\": \"2.0\", \"id\": mid, \"result\": {\"tools\": TOOLS}})\n"
    "    elif method == \"tools/call\":\n"
    "        text = (req.get(\"params\") or {}).get(\"arguments\", {}).get(\"text\", \"\")\n"
    "        send({\"jsonrpc\": \"2.0\", \"id\": mid, \"result\": {\"content\": [{\"type\": \"text\", \"text\": \"stub says: \" + str(text)}]}})\n"
    "    else:\n"
    "        send({\"jsonrpc\": \"2.0\", \"id\": mid, \"error\": {\"code\": -32601, \"message\": \"nope\"}})\n"
)


class _Tok:
    def apply_chat_template(self, messages, tools=None,
                            add_generation_prompt=False, enable_thinking=False):
        return list(range(sum(len(m.get("content", "")) for m in messages) // 4 + 8))

    def decode(self, ids, skip_special_tokens=False):
        return ""


def _tcall(name, **args):
    return "<tool_call>\n" + json.dumps({"name": name, "arguments": args}) + "\n</tool_call>"


class _Scripted:
    def __init__(self, script):
        self.script = list(script)
        self.model_id = "scripted"
        self.effective_ctx = 24000
        self.cache_dir = None
        self._cached_ids = []
        self.tok = _Tok()

    def generate(self, prompt_ids, max_tokens=2048, on_token=None,
                 stop_texts=None, should_stop=None, on_prefill=None,
                 on_prefill_progress=None, stop_condition=None,
                 think_ceiling=None):
        text = self.script.pop(0)
        if on_token:
            on_token(text)
        return text, GenStats(prompt_tokens=len(prompt_ids))

    def reset(self):
        self._cached_ids = []

    def warm_prefix(self, prefix_ids, should_stop=None, head_ids=None):
        return "skip", 0


def test_client_mcp_end_to_end(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    stub = tmp_path / "stub_mcp.py"
    stub.write_text(_MCP_STUB)
    target = tmp_path / "note.txt"
    script = [
        _tcall("mcp__stub__echo", text="hi"),
        _tcall("write", path=str(target), content="hi\n"),
        _tcall("bash", command="cat " + str(target)),
        _tcall("done", summary="ok"),
    ]

    def make_agent(cwd, mode, emit, confirm, should_stop, tool_event,
                   session_id=None):
        return Agent(_Scripted(script), mode=mode, thinking=False, emit=emit,
                     confirm=confirm, should_stop=should_stop,
                     tool_event=tool_event, persist=False)

    rig = Rig("echo", default_mode="yolo")
    rig.server._make_agent = make_agent
    try:
        rig.initialize()
        servers = [{"name": "stub", "command": sys.executable,
                    "args": [str(stub)]}]
        rid = rig.request("session/new", {"cwd": str(tmp_path),
                                          "mcpServers": servers})
        msg, _ = rig.expect_response(rid)
        session_id = msg["result"]["sessionId"]
        rid = rig.request("session/prompt",
                          {"sessionId": session_id,
                           "prompt": [{"type": "text", "text": "go"}]})
        msg, notes = rig.expect_response(rid, timeout=60.0)
        assert msg["result"]["stopReason"] == "end_turn"
        calls = _updates(notes, "tool_call")
        assert calls[0]["title"] == "MCP stub / echo"
        first = _updates(notes, "tool_call_update")[0]
        assert first["status"] == "completed"
        assert "stub says: hi" in first["content"][0]["content"]["text"]
        assert any("MCP:" in c["content"]["text"]
                   for c in _updates(notes, "agent_message_chunk"))
        assert target.read_text() == "hi\n"
    finally:
        mcp.set_client_servers(str(tmp_path), [])
        mcp.reset_session()
        rig.close()


def _no_builtin_fixture(tmp_path, monkeypatch, server="bridge"):
    from chad import tools as _tools
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    mcp.reset_session()
    stub = tmp_path / "nb_stub.py"
    stub.write_text(_MCP_STUB)
    warnings = mcp.set_client_servers(
        str(tmp_path), [(server, {"command": sys.executable,
                                  "args": [str(stub)]})])
    assert warnings == []
    _tools.set_name_overlay({
        "bash": "mcp__" + server + "__do",
        "read": "mcp__" + server + "__echo",
    })
    return mcp


def test_overlay_never_auto_approves(tmp_path, monkeypatch):
    from chad import tools as _tools
    from chad.agent import auto_approves
    _no_builtin_fixture(tmp_path, monkeypatch)
    try:
        assert auto_approves("yolo", "bash") is False
        assert auto_approves("auto", "bash") is False
        assert auto_approves("normal", "bash") is False
        _tools.set_name_overlay({})
        assert auto_approves("yolo", "bash") is True
    finally:
        _tools.set_name_overlay({})
        mcp.set_client_servers(str(tmp_path), [])
        mcp.reset_session()


def test_yolo_still_asks_for_overlay_tools(tmp_path, monkeypatch):
    asked = []
    _no_builtin_fixture(tmp_path, monkeypatch)
    try:
        agent = Agent(
            _Scripted([_tcall("bash", text="hi"),
                       _tcall("done", summary="ok")]),
            mode="yolo", thinking=False,
            confirm=lambda n, a: asked.append(n) or True)
        agent.run_turn("run it")
        assert asked == ["bash"]
        tool_texts = [m.get("content", "") for m in agent.messages
                      if m.get("role") == "tool"]
        assert any("stub says: hi" in t for t in tool_texts)
    finally:
        from chad import tools as _tools
        _tools.set_name_overlay({})
        mcp.set_client_servers(str(tmp_path), [])
        mcp.reset_session()


def test_acp_no_builtins_flag():
    from chad import cli as _cli
    assert _cli._acp_parser().parse_args([]).no_builtins is None
    assert _cli._acp_parser().parse_args(["--no-builtins"]).no_builtins == "local"
    parsed = _cli._acp_parser().parse_args(["--no-builtins", "edge"])
    assert parsed.no_builtins == "edge"
def test_bridge_mode_forwards_url_servers_only(tmp_path):
    rig = Rig("echo", connect_client_servers=False)
    try:
        rig.initialize()
        rid = rig.request("session/new", {
            "cwd": str(tmp_path),
            "mcpServers": [
                {"name": "ctx", "type": "http",
                 "url": "http://example.invalid/mcp",
                 "headers": [{"name": "A", "value": "b"}]},
                {"name": "sse-feed", "type": "sse",
                 "url": "http://example.invalid/sse"},
                {"name": "editor-cmd", "command": "do-thing",
                 "args": []},
            ]})
        msg, _ = rig.expect_response(rid)
        sid = msg["result"]["sessionId"]
        assert rig.server.session_client_servers(sid) == [
            {"name": "ctx", "type": "http",
             "url": "http://example.invalid/mcp",
             "headers": [{"name": "A", "value": "b"}]}]
        assert rig.server.session_client_servers("missing") == []
        with rig.server._sessions_lock:
            notes = list(rig.server._sessions[sid].mcp_notes)
        assert any("editor-cmd" in n and "do not cross" in n for n in notes)
    finally:
        rig.close()
def test_prompts_share_one_worker_thread(tmp_path):
    # The MLX engine binds streams to the thread that first ran it; a fresh
    # thread per prompt crashed the second live turn with
    # `no Stream(gpu, 1) in current thread`, so every turn must run on the
    # same worker.
    rig = Rig("echo")
    try:
        rig.initialize()
        msg, _ = rig.new_session(tmp_path)
        sid = msg["result"]["sessionId"]
        for word in ("one", "two"):
            rid = rig.request("session/prompt", {
                "sessionId": sid,
                "prompt": [{"type": "text", "text": word}]})
            msg, _ = rig.expect_response(rid)
            assert msg["result"]["stopReason"] == "end_turn"
        idents = rig.created[0].thread_idents
        assert len(idents) == 2
        assert idents[0] == idents[1]
    finally:
        rig.close()
