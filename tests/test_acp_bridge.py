"""Tests for the ACP bridge relay (src/chad/acp_bridge.py).

Pure relay rules plus ssh spawn shape. The three-tier run lives in
tests/acp_client/run-bridge.mjs (official client, scripted remote).
"""

import subprocess

from chad.acp_bridge import (
    RemoteProxy,
    _ssh_exec,
    build_ssh_argv,
    caps_for,
    clamp_mode,
    map_permission_answer,
    remote_acp_argv,
    remote_scratch,
)


def test_clamp_mode():
    assert clamp_mode("yolo") == "auto"
    assert clamp_mode("normal") == "normal"
    assert clamp_mode("auto") == "auto"
    assert clamp_mode("plan") == "plan"


def test_map_permission_answer():
    selected = lambda oid: {"outcome": {"outcome": "selected",
                                        "optionId": oid}}
    assert map_permission_answer(selected("allow-once")) == {
        "outcome": "selected", "optionId": "allow-once"}
    assert map_permission_answer(selected("reject-once")) == {
        "outcome": "selected", "optionId": "reject-once"}
    assert map_permission_answer(selected("allow-always")) == {
        "outcome": "selected", "optionId": "allow-once"}
    assert map_permission_answer(selected("Always Allow")) == {
        "outcome": "selected", "optionId": "allow-once"}
    assert map_permission_answer(selected("nope")) == {
        "outcome": "selected", "optionId": "reject-once"}
    assert map_permission_answer({"outcome": {"outcome": "cancelled"}}) == {
        "outcome": "cancelled"}
    assert map_permission_answer({}) == {
        "outcome": "selected", "optionId": "reject-once"}
    assert map_permission_answer(None) == {
        "outcome": "selected", "optionId": "reject-once"}


def test_caps_for():
    assert caps_for({}) == {"read": False, "write": False, "terminal": False}
    caps = caps_for({"fs": {"readTextFile": True, "writeTextFile": True},
                     "terminal": True})
    assert caps == {"read": True, "write": True, "terminal": True}
    caps = caps_for({"fs": {"readTextFile": True}})
    assert caps == {"read": True, "write": False, "terminal": False}


def test_build_ssh_argv_shape():
    argv = build_ssh_argv("me@box", 18789, 51234, [],
                          ["chad", "acp", "--no-builtins", "local"])
    assert argv[0] == "ssh"
    assert "-R" in argv
    assert "18789:127.0.0.1:51234" in argv
    assert argv[-2] == "me@box"
    tail = argv[-1]
    assert "chad acp --no-builtins local" in tail
    assert "$HOME/.local/bin" in tail
    assert "command -v chad" in tail
    assert "exec " in tail
    assert "ExitOnForwardFailure=yes" in argv
    assert "BatchMode=yes" in argv


def test_build_ssh_argv_quotes_env():
    argv = build_ssh_argv("me@box", 1, 2, ["A=b c"], ["chad"])
    tail = argv[-1]
    assert "env " in tail
    assert "b c" in tail
    assert "command -v chad" in tail


def test_build_ssh_argv_user_path_wins():
    argv = build_ssh_argv("me@box", 1, 2, ["PATH=/opt/x/bin:/usr/bin"],
                          ["chad"])
    tail = argv[-1]
    assert "$HOME/.local/bin" not in tail
    assert "/opt/x/bin" in tail
    assert "command -v chad" in tail


def test_build_ssh_argv_explicit_path_skips_guard():
    argv = build_ssh_argv("me@box", 1, 2, [], ["/usr/local/bin/chad"])
    assert argv[-1] == "/usr/local/bin/chad"


def test_remote_acp_argv_log_file():
    assert remote_acp_argv(None) == ["chad", "acp", "--no-builtins", "local"]
    argv = remote_acp_argv(".chad/logs/acp-bridge-remote-x.log")
    assert argv[-2:] == ["--log-file", ".chad/logs/acp-bridge-remote-x.log"]
    tail = build_ssh_argv("me@box", 1, 2, [], argv)[-1]
    assert "--log-file" in tail
    assert "exec chad acp --no-builtins local" in tail


class _RecordRun:
    """Injectable _ssh_exec seam: records kwargs, answers empty success."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_ssh_exec_without_input_gets_devnull_stdin():
    # Regression: an inheriting ssh forwards Zed's next stdin bytes to the
    # remote command, stealing them from the bridge's ACP stream (a prompt
    # sent while a provision exec is alive hangs, then errors).
    rec = _RecordRun()
    code, out, err = _ssh_exec("me@box", "true", _run=rec)
    assert (code, out, err) == (0, b"", b"")
    assert rec.calls[0][1]["stdin"] == subprocess.DEVNULL
    assert "input" not in rec.calls[0][1]


def test_ssh_exec_with_input_pipes_bytes():
    rec = _RecordRun()
    _ssh_exec("me@box", "cat", input_bytes=b"hi", _run=rec)
    assert rec.calls[0][1]["input"] == b"hi"
    assert "stdin" not in rec.calls[0][1]


def test_remote_proxy_stop_mapping():
    seen = {}

    def fake_turn(text):
        seen["text"] = text
        return "end_turn"

    proxy = RemoteProxy(fake_turn, lambda v: seen.setdefault("mode", v),
                        "normal")
    assert proxy.run_turn("hi") == ""
    assert seen["text"] == "hi"
    assert proxy.interrupted is False
    assert proxy.budget_note is None
    proxy.mode = "yolo"
    assert proxy.mode == "yolo"
    assert seen["mode"] == "yolo"

    def fake_cancel(text):
        return "cancelled"

    proxy2 = RemoteProxy(fake_cancel, lambda v: None, "normal")
    proxy2.run_turn("hi")
    assert proxy2.interrupted is True

    def fake_budget(text):
        return "max_turn_requests"

    proxy3 = RemoteProxy(fake_budget, lambda v: None, "normal")
    proxy3.run_turn("hi")
    assert proxy3.budget_note is not None
def test_remote_scratch_is_home_relative():
    # The remote session/new checks the literal path (no tilde expansion),
    # so the scratch must stay home-relative for provision_session to join
    # against the remote $HOME (live loopback caught the tilde variant).
    assert remote_scratch("abc-123_ Brennan") == ".chad/bridge/abc-123_Brennan"
    assert "~" not in remote_scratch("s")
def test_permission_answer_nests_under_outcome():
    # Regression (caught live): the bridge once sent the bare outcome object
    # as the permission `result`; the remote parsed it as unknown shape and
    # denied every mutating tool. The full answer must nest under `outcome`.
    from chad import acp
    zed = {"outcome": {"outcome": "selected", "optionId": "allow-once"}}
    full = {"outcome": map_permission_answer(zed)}
    assert full == {"outcome": {"outcome": "selected",
                                  "optionId": "allow-once"}}
    server = acp.AcpServer(lambda: None, lambda line: None,
                           lambda *a, **k: None)
    try:
        rec = acp._AcpSession(session_id="s", cwd="/tmp",
                              start_mode="normal")
        server._pending[7] = acp._Pending()
        server._pending[7].result = dict(full)
        server._pending[7].event.set()
        assert server._await_permission(7, rec) == "allow-once"
        flat = dict(map_permission_answer(zed))
        server._pending[8] = acp._Pending()
        server._pending[8].result = flat
        server._pending[8].event.set()
        assert server._await_permission(8, rec) == "reject-once"
    finally:
        server._running = False
