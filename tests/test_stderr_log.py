"""Tests for the stderr file log (src/chad/stderr_log.py).

The tee duplicates Zed-visible stderr into ~/.chad/logs without touching
protocol stdout. Every test relocates home state into tmp dirs: nothing
here may write the developer's real ~/.chad.
"""

import io
import os
import sys

import pytest

from chad import stderr_log


@pytest.fixture
def log_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAD_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("CHAD_SESSION_LOG", raising=False)
    monkeypatch.delenv("CHAD_NO_SESSION_LOG", raising=False)
    return tmp_path


def test_tee_duplicates_to_file_and_stream():
    stream = io.StringIO()
    path = "/tmp/chad-tee-probe.log"
    if os.path.exists(path):
        os.remove(path)
    try:
        tee = stderr_log._Tee(stream, path)
        assert tee.write("hello\n") == len("hello\n")
        tee.flush()
        assert stream.getvalue() == "hello\n"
        with open(path, encoding="utf-8") as f:
            assert f.read() == "hello\n"
    finally:
        if os.path.exists(path):
            os.remove(path)


def test_install_explicit_path_creates_parents(log_home):
    path = str(log_home / "logs" / "deep" / "acp.log")
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        assert stderr_log.install("acp", path) == path
        assert os.path.exists(path)
        sys.stderr.write("line\n")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "logging stderr to" in content
        assert "line\n" in content
    finally:
        sys.stderr = old


def test_install_without_opt_in_stays_quiet(log_home):
    assert stderr_log.install("acp", None) is None


def test_install_follows_session_log_opt_in(log_home, monkeypatch):
    monkeypatch.setenv("CHAD_SESSION_LOG", "1")
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        path = stderr_log.install("acp-bridge", None)
        assert path is not None
        assert path.startswith(str(log_home / "logs"))
        assert "acp-bridge" in os.path.basename(path)
    finally:
        sys.stderr = old


def test_install_unwritable_path_warns_and_continues(log_home):
    blocked = log_home / "blocked"
    blocked.mkdir()
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        assert stderr_log.install("acp", str(blocked)) is None
        assert "unavailable" in sys.stderr.getvalue()
    finally:
        sys.stderr = old


def test_default_path_shape(log_home):
    path = stderr_log.default_path("acp")
    assert path.startswith(str(log_home / "logs"))
    base = os.path.basename(path)
    assert base.startswith("acp-")
    assert base.endswith("-%d.log" % (os.getpid(),))
