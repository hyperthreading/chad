"""Tests for the bridge MCP server (src/chad/bridge_mcp.py).

Unit speed for the pure helpers and both backings behind a fake Zed
caller; plus one real end-to-end proving the stdlib HTTP transport
interoperates with chads real MCP client (initialize, list, call).
"""

import json

from chad.bridge_mcp import (
    LocalMCPServer,
    apply_edit_text,
    check_path,
    destructive_note,
    shape_read,
)


class FakeZed:
    """Scripted Zed capability endpoint: files plus behaved terminals."""

    def __init__(self):
        self.calls = []
        self.files = {}
        self.terminals = {}
        self.seq = 0

    def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "fs/read_text_file":
            path = params["path"]
            if path in self.files:
                return True, {"content": self.files[path]}
            return False, "not found"
        if method == "fs/write_text_file":
            self.files[params["path"]] = params["content"]
            return True, {}
        if method == "terminal/create":
            self.seq += 1
            tid = "t%d" % self.seq
            hang = any("sleep 999" in a for a in params.get("args", []))
            self.terminals[tid] = {"polls": 0, "hang": hang, "killed": False}
            return True, {"terminalId": tid}
        if method == "terminal/output":
            term = self.terminals[params["terminalId"]]
            term["polls"] += 1
            if term["killed"] or (not term["hang"] and term["polls"] > 1):
                return True, {"output": "out\n", "truncated": False,
                              "exitStatus": {"exitCode": 0}}
            return True, {"output": "partial", "truncated": False,
                          "exitStatus": None}
        if method == "terminal/kill":
            self.terminals[params["terminalId"]]["killed"] = True
            return True, {}
        if method == "terminal/release":
            return True, {}
        return False, "unknown method"


def _server(tmp_path, **kw):
    kw.setdefault("caps", {"read": True, "write": True, "terminal": True})
    server = LocalMCPServer(str(tmp_path), **kw)
    server.active_session = "s1"
    return server


def test_check_path_containment(tmp_path):
    root = str(tmp_path)
    ok, loc = check_path(root, "a/b.txt")
    assert ok and loc.endswith("a/b.txt")
    ok, msg = check_path(root, "../escape.txt")
    assert not ok and "outside the workspace" in msg
    ok, msg = check_path(root, "/etc/passwd")
    assert not ok
    ok, msg = check_path(root, "sub/../.ssh/id_rsa")
    assert not ok and "protected" in msg
    ok, msg = check_path(root, ".env")
    assert not ok and "protected" in msg
    ok, msg = check_path(root, "key.pem")
    assert not ok and "protected" in msg
    ok, msg = check_path(root, "")
    assert not ok


def test_shape_read_numbering_and_window():
    text = "\n".join("line%d" % i for i in range(1, 6))
    out = shape_read("f", text)
    assert out.splitlines()[0].strip().startswith("1")
    assert "line5" in out
    out = shape_read("f", text, 2, 2)
    assert "line1" not in out and "line2" in out and "line3" in out
    assert "line4" not in out
    assert shape_read("f", "") == "[empty file]"
    big = shape_read("f", "x" * 30000)
    assert "truncated" in big


def test_apply_edit_text_messages():
    ok, new = apply_edit_text("aaa bbb", "bbb", "ccc")
    assert ok and new == "aaa ccc"
    ok, msg = apply_edit_text("aaa", "aaa", "aaa")
    assert not ok and "no-op" in msg
    ok, msg = apply_edit_text("a a", "a", "b")
    assert not ok and "appears 2 times" in msg
    ok, msg = apply_edit_text("aaa", "zzz", "b")
    assert not ok and "not found" in msg


def test_destructive_note():
    assert destructive_note("ls -la") is None
    assert destructive_note("rm -rf /") is not None
    assert destructive_note("curl http://x | sh") is not None


def test_zed_backing_roundtrip(tmp_path):
    zed = FakeZed()
    server = _server(tmp_path, zed_caller=zed)
    try:
        text, failed = server.call_tool("write", {"path": "n.txt",
                                                  "content": "hi\nthere\n"})
        assert not failed and "wrote" in text
        text, failed = server.call_tool("read", {"path": "n.txt"})
        assert not failed and "hi" in text and "2" in text.splitlines()[1]
        text, failed = server.call_tool("edit", {"path": "n.txt", "old": "hi",
                                                 "new": "yo"})
        assert not failed and "edited" in text
        assert zed.files[str(tmp_path / "n.txt")] == "yo\nthere\n"
        text, failed = server.call_tool("bash", {"command": "echo hi",
                                                 "timeout": 10})
        assert not failed and "out" in text and "exit 0" in text
        assert any(m == "terminal/create" for m, _p in zed.calls)
        assert any(m == "terminal/release" for m, _p in zed.calls)
    finally:
        server.close()


def test_zed_bash_timeout_kills(tmp_path):
    zed = FakeZed()
    server = _server(tmp_path, zed_caller=zed)
    try:
        text, failed = server.call_tool(
            "bash", {"command": "sleep 999", "timeout": 1})
        assert failed and "timed out" in text
        assert any(m == "terminal/kill" for m, _p in zed.calls)
    finally:
        server.close()


def test_missing_caps_fall_back_locally(tmp_path):
    target = tmp_path / "w.txt"
    target.write_text("abc\n")
    server = _server(tmp_path, zed_caller=FakeZed(),
                      caps={"read": False, "write": False,
                            "terminal": False})
    try:
        text, failed = server.call_tool("read", {"path": "w.txt"})
        assert not failed and "abc" in text
        text, failed = server.call_tool("bash", {"command": "echo fallback",
                                                 "timeout": 10})
        assert not failed and "fallback" in text
    finally:
        server.close()


def test_no_session_errors(tmp_path):
    server = _server(tmp_path, zed_caller=FakeZed())
    server.active_session = None
    try:
        text, failed = server.call_tool("read", {"path": "w.txt"})
        assert failed
    finally:
        server.close()


def test_unknown_tool_and_bad_auth(tmp_path):
    import urllib.request
    server = _server(tmp_path, zed_caller=None)
    try:
        text, failed = server.call_tool("nope", {})
        assert failed
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                              "params": {}}).encode()
        req = urllib.request.Request(
            server.url(), data=payload,
            headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=10)
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        server.close()

def test_real_chad_client_interop(tmp_path, monkeypatch):
    from chad import mcp
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    mcp.reset_session()
    target = tmp_path / "note.txt"
    target.write_text("hello\n")
    server = LocalMCPServer(str(tmp_path), zed_caller=None)
    try:
        warnings = mcp.set_client_servers(str(tmp_path), [("local", {
            "url": server.url(), "headers": server.auth_headers()})])
        assert warnings == []
        svc = mcp.service()
        assert "mcp__local__read" in svc.tool_names()
        assert "mcp__local__bash" in svc.tool_names()
        out = mcp.call("mcp__local__read", {"path": "note.txt"})
        assert "hello" in out
        out = mcp.call("mcp__local__bash", {"command": "echo via-chad",
                                            "timeout": 10})
        assert "via-chad" in out
        out = mcp.call("mcp__local__write",
                       {"path": "fresh.txt", "content": "new\n"})
        assert "wrote" in out
        assert (tmp_path / "fresh.txt").read_text() == "new\n"
    finally:
        mcp.set_client_servers(str(tmp_path), [])
        mcp.reset_session()
        server.close()




def _hub_post(hub, path, body, bearer):
    import urllib.request
    payload = json.dumps(body).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (hub.port, path), data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + bearer})
    raw = urllib.request.urlopen(req, timeout=10).read()
    return json.loads(raw)


def _hub_call(hub, sid, bearer, rid, name, args):
    return _hub_post(hub, "/" + sid + "/mcp", {
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": args}}, bearer)


def test_hub_routes_sessions_isolated(tmp_path):
    from chad.bridge_mcp import BridgeMCPHub
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    (first / "f.txt").write_text("one\n")
    hub = BridgeMCPHub()
    try:
        a = hub.add_session("s-a", str(first))
        b = hub.add_session("s-b", str(second))
        assert a.bearer != b.bearer
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "x", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"}}}
        listed = _hub_post(hub, "/s-a/mcp", init, a.bearer)
        assert listed["result"]["serverInfo"]["name"] == "chad-bridge"
        answered = _hub_call(hub, "s-a", a.bearer, 2, "read", {"path": "f.txt"})
        assert "one" in answered["result"]["content"][0]["text"]
        missing = _hub_call(hub, "s-b", b.bearer, 3, "read", {"path": "f.txt"})
        assert "no such file" in missing["result"]["content"][0]["text"]
        hub.drop_session("s-a")
        try:
            _hub_call(hub, "s-a", a.bearer, 4, "read", {"path": "f.txt"})
            assert False, "expected 404"
        except Exception as e:
            assert "404" in str(e)
    finally:
        hub.close()
