"""Tee process stderr to a file for `chad acp` and `chad acp-bridge`.

Protocol traffic stays on stdout; everything already aimed at stderr
(Zed-visible status lines, `[remote]`-prefixed remote output) is
duplicated into a log file so a failed turn can be diagnosed after the
fact. An explicit `--log-file` always logs; otherwise logging follows
the `CHAD_SESSION_LOG` opt-in in `config.traces_enabled`, so
unattended runs do not grow plaintext traces by default. A log file
that cannot be opened never aborts startup: the process keeps stderr
and says so once.
"""

import os
import sys
import threading
import time

from . import config


def log_dir() -> str:
    """The log directory: `CHAD_LOG_DIR` when set, else ~/.chad/logs.

    Read on every call so a test can relocate it without touching home
    state, mirroring `session.store_dir`.
    """
    return config.env_str("CHAD_LOG_DIR") or os.path.expanduser("~/.chad/logs")


def default_path(kind: str) -> str:
    """A fresh log path for one process: kind, local time, pid."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    return os.path.join(log_dir(), "%s-%s-%d.log" % (kind, stamp, os.getpid()))


class _Tee:
    """Duplicate every stderr write to a log file.

    The bridge writes stderr from its reader, driver and server threads
    at once, so both streams move under one lock. The original stream
    keeps its exact timing (no added flush); only the file is flushed
    per write so a killed process still leaves a readable tail.
    """

    def __init__(self, stream, path: str) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._file = open(path, "a", encoding="utf-8")
        self.path = path

    def write(self, text: str) -> int:
        with self._lock:
            self._stream.write(text)
            self._file.write(text)
            self._file.flush()
        return len(text)

    def flush(self) -> None:
        with self._lock:
            self._stream.flush()
            self._file.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self) -> str:
        return self._stream.encoding


def install(kind: str, path_arg: str | None) -> str | None:
    """Tee `sys.stderr` to a log file; returns the path, or None.

    `kind` names the process in the default filename (`acp`,
    `acp-bridge`). An explicit path always wins; without one, logging
    happens only under `CHAD_SESSION_LOG`. The resolved path is
    announced on stderr itself, so the Zed log shows where the file
    went — including the remote side, whose line arrives `[remote]`-prefixed.
    """
    path = path_arg or (default_path(kind) if config.traces_enabled() else None)
    if path is None:
        return None
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tee = _Tee(sys.stderr, path)
    except OSError as e:
        sys.stderr.write("chad %s: log file %s unavailable (%s); stderr only\n"
                         % (kind, path, e))
        return None
    # _Tee carries the stderr surface this codebase uses
    # (write/flush/isatty/fileno/encoding); nothing here redirects stderr
    # further, so the narrower type never leaks.
    sys.stderr = tee
    sys.stderr.write("chad %s: logging stderr to %s\n" % (kind, tee.path))
    return tee.path
