"""Characterization battery for the filesystem tools in `tools.py`:
`tool_edit` (uniqueness/no-corruption truth table) and `tool_write`. Pure filesystem
in a temp dir — no model load.

The KEY invariant: `tool_edit` rewrites the file ONLY on a genuinely-unique match and
leaves it BYTE-FOR-BYTE untouched in every reject/ambiguous case. `test_edit.py` already
covers the recovery cascade (literal \\n, indent drift, ws-flexible ambiguity); this file
focuses on the write table plus the core edit truth table.

Run: `.venv/bin/python test_tools.py`
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chad import tools  # noqa: E402

PASS = 0
FAIL = 0


def _with_lever_off(name):
    """Run a test with one lever subtracted from the suite's CHAD_ENABLE=all — for
    tests that pin the legacy (lever-OFF) output shape while the rest of the suite
    exercises everything ON."""
    import functools

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **k):
            old = os.environ.get("CHAD_DISABLE")
            os.environ["CHAD_DISABLE"] = f"{old},{name}" if old else name
            try:
                return fn(*a, **k)
            finally:
                if old is None:
                    os.environ.pop("CHAD_DISABLE", None)
                else:
                    os.environ["CHAD_DISABLE"] = old
        return wrapper
    return deco


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        raise AssertionError(f"{name}  {detail}")


def _seed(files):
    """Create a fresh temp dir, write `files` (name->content, nested ok), chdir in.
    Caller MUST restore cwd in a finally."""
    d = tempfile.mkdtemp(prefix="tools_")
    for fn, c in files.items():
        p = os.path.join(d, fn)
        if os.path.dirname(fn):
            os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(c.encode())
    os.chdir(d)
    return d


def _rawbytes(path):
    with open(path, "rb") as f:
        return f.read()


# --- tool_edit truth table ----------------------------------------------------

def test_edit_truth_table():
    cwd = os.getcwd()
    try:
        _seed({"f.py": "alpha\nbeta\ngamma\n"})

        # missing file
        check("edit: missing file",
              tools.tool_edit("nope.py", "a", "b").startswith("[no such file"))

        # old == new -> no-op, file untouched
        before = _rawbytes("f.py")
        res = tools.tool_edit("f.py", "beta", "beta")
        check("edit: no-op old==new", res.startswith("[no-op edit"), res)
        check("edit: no-op leaves file byte-identical", _rawbytes("f.py") == before)

        # old absent (and not whitespace-recoverable) -> not found, file untouched
        before = _rawbytes("f.py")
        res = tools.tool_edit("f.py", "qqq_absent_zzz", "whatever")
        check("edit: not found", res.startswith("[old string not found"), res)
        check("edit: not-found leaves file byte-identical", _rawbytes("f.py") == before)

        # old appears twice exactly -> ambiguous, file untouched
        _seed({"d.py": "x = 1\nx = 1\n"})
        before = _rawbytes("d.py")
        res = tools.tool_edit("d.py", "x = 1", "x = 9")
        check("edit: ambiguous n>1", res.startswith("[old string appears 2 times"), res)
        check("edit: ambiguous leaves file byte-identical", _rawbytes("d.py") == before)

        # unique match -> edited, file ACTUALLY rewritten
        _seed({"u.py": "alpha\nbeta\ngamma\n"})
        res = tools.tool_edit("u.py", "beta", "BETA")
        check("edit: unique returns [edited", res.startswith("[edited"), res)
        after = _rawbytes("u.py").decode()
        check("edit: unique applied the write", "BETA" in after and "beta" not in after, repr(after))
    finally:
        os.chdir(cwd)


def test_edit_refuses_a_file_that_is_not_utf8():
    """Reading with `errors="replace"` would hand the edit a U+FFFD where the odd byte
    was, and the rewrite would then persist it — one edit corrupting bytes it never
    touched. Refuse the file instead, and leave it exactly as it was."""
    cwd = os.getcwd()
    try:
        _seed({})
        raw = "caf\xe9 = 1\nx = 2\n".encode("latin-1")
        with open("l1.py", "wb") as f:
            f.write(raw)
        res = tools.tool_edit("l1.py", "x = 2", "x = 3")
        check("edit: refuses a non-utf-8 file", res.startswith("[cannot edit"), res)
        check("edit: names the way through", "use bash" in res, res)
        check("edit: refused file is byte-identical", _rawbytes("l1.py") == raw)
    finally:
        os.chdir(cwd)


def test_file_tools_pin_utf8_instead_of_the_locale():
    """Every file open in `tools.py` names UTF-8. With the locale encoding instead, a
    container that sets no LANG (the documented --backend llama path) cannot read or
    write a source file with a single non-ASCII character in it."""
    import builtins
    cwd = os.getcwd()
    real_open = builtins.open
    seen = []

    def spy(file, mode="r", *a, **kw):
        # Only the opens tools.py itself makes; other modules have their own contract.
        if sys._getframe(1).f_globals.get("__name__") == "chad.tools" and "b" not in mode:
            seen.append((mode, kw.get("encoding")))
        return real_open(file, mode, *a, **kw)

    try:
        _seed({"u.py": "café = 1\n"})
        builtins.open = spy
        tools.tool_write("w.py", "s = 'héllo'\n")
        tools.tool_edit("u.py", "café", "thé")
        builtins.open = real_open
        check("io: tools.py opened files", len(seen) >= 3, seen)
        check("io: every text open names utf-8", all(e == "utf-8" for _m, e in seen), seen)
        check("edit: non-ascii content round-trips",
              _rawbytes("u.py").decode("utf-8") == "thé = 1\n")
        check("write: non-ascii content round-trips",
              _rawbytes("w.py").decode("utf-8") == "s = 'héllo'\n")
    finally:
        builtins.open = real_open
        os.chdir(cwd)


# --- tool_bash ----------------------------------------------------------------

# Line clipping ablated: every oversized fixture here is one 40k-char blob, and
# this test pins the head/tail budget's shape on it. test_bash_line_clip covers the
# clip path, including that a blob is what it exists for.
@_with_lever_off("bash_line_clip")
def test_bash():
    # short output passes through untouched
    check("bash: short echo", tools.tool_bash("printf hi") == "hi")

    # no output -> sentinel
    check("bash: no output", tools.tool_bash("true") == "[no output]")

    # nonzero exit -> [exit N] prefix
    res = tools.tool_bash("printf oops; exit 3")
    check("bash: exit prefix", res.startswith("[exit 3]\n") and "oops" in res, res)

    # The timeout-KILL path: timeout with no output before the kill -> bare
    # sentinel (0s deadline fires immediately, before `sleep` prints anything).
    check("bash: timeout no output", tools.tool_bash("sleep 5", timeout=0)
          == "[timed out after 0s; no output before it was killed]")

    # timeout WITH partial output -> the output the process printed before the kill
    # is preserved (the whole point: a killed build/train still printed how far it
    # got). Print a marker, then sleep past a short deadline so the kill lands
    # mid-run.
    partial = tools.tool_bash("printf 'PROGRESS_50_PERCENT\\n'; sleep 5", timeout=1)
    check("bash: timeout keeps partial", "PROGRESS_50_PERCENT" in partial, partial)
    check("bash: timeout names the kill", partial.startswith("[timed out after 1s;"),
          partial[:60])

    # interrupt (should_stop) also preserves partial output
    stop = {"n": 0}
    def should_stop():
        stop["n"] += 1
        return stop["n"] > 3  # let a couple poll cycles pass so the printf lands first
    intr = tools.tool_bash("printf 'PARTIAL_BEFORE_CTRLC\\n'; sleep 5", should_stop=should_stop)
    check("bash: interrupt keeps partial", "PARTIAL_BEFORE_CTRLC" in intr, intr)
    check("bash: interrupt names the stop", intr.startswith("[interrupted by user;"), intr[:60])

    # long output keeps HEAD + TAIL + an omission marker, and the tail bias means
    # the last line (a failure summary lives here) survives when a head-only cut
    # would drop it.
    big = tools.tool_bash(
        "printf 'FIRST_LINE\\n'; head -c 40000 /dev/zero | tr '\\0' 'x'; "
        "printf '\\nLAST_LINE_FAILURE_SUMMARY\\n'")
    check("bash: long output truncated", len(big) < 25000, len(big))
    check("bash: keeps head", "FIRST_LINE" in big, big[:80])
    check("bash: keeps tail", "LAST_LINE_FAILURE_SUMMARY" in big, big[-80:])
    check("bash: omission marker", "chars omitted" in big, big)

    # the [exit N] prefix survives truncation (it's at the head we keep)
    fail = tools.tool_bash(
        "printf 'HEAD\\n'; head -c 40000 /dev/zero | tr '\\0' 'x'; "
        "printf '\\nTAIL\\n'; exit 1")
    check("bash: exit prefix survives truncation", fail.startswith("[exit 1]\n"), fail[:40])


def _spill_path_from(result):
    """Extract the spill path the truncation notice points at."""
    return result.split("FULL output saved to ", 1)[1].split(";", 1)[0]


@_with_lever_off("bash_trim_keep_failures")
def test_bash_line_clip():
    """One absurdly long line is capped instead of eating the whole budget, and the
    full text stays recoverable. The measured case: a search that reached a gitignored
    build directory returned a 92k-char .js.map line, and head+tail handed back 20k
    chars sliced out of the middle of base64 — ~5k tokens of prefill, no information."""
    old = os.environ.get("CHAD_SPILL_DIR")
    d = tempfile.mkdtemp(prefix="clip_")
    os.environ["CHAD_SPILL_DIR"] = d
    try:
        blob = "M" * 92000
        out = tools._bash_headtail(f"head.js.map:1:{blob}\nreal line\n")
        check("clip: result is small", len(out) < 1200, len(out))
        check("clip: keeps the line's start", out.startswith("head.js.map:1:MMM"), out[:40])
        check("clip: marks the line", "…[line clipped]" in out, out[:800])
        check("clip: other lines intact", "real line" in out, out)
        check("clip: names the amount", "chars clipped from over-long lines" in out, out)
        with open(_spill_path_from(out)) as f:
            full = f.read()
        check("clip: spill holds the UNCLIPPED original", blob in full, len(full))

        # a small clip is not worth a file; the notice just says what happened
        small = tools._bash_headtail("x" * (tools.BASH_LINE_CHARS + 50) + "\n")
        check("clip: small clip has no spill", "saved to" not in small, small[-120:])
        check("clip: small clip still marked", "…[line clipped]" in small, small[-120:])

        # normal line-shaped output is byte-identical
        plain = "line one\nline two\n"
        check("clip: normal output untouched", tools._bash_headtail(plain) == plain)
    finally:
        if old is None:
            os.environ.pop("CHAD_SPILL_DIR", None)
        else:
            os.environ["CHAD_SPILL_DIR"] = old


@_with_lever_off("bash_line_clip")  # single-blob fixtures; see test_bash
def test_bash_spill():
    """Truncation spills the FULL output to a session-scoped file: the
    omitted middle is a `grep <path>` away instead of a full re-run away."""
    old = os.environ.get("CHAD_SPILL_DIR")
    d = tempfile.mkdtemp(prefix="spill_")
    os.environ["CHAD_SPILL_DIR"] = d
    session_dir = os.path.join(d, str(os.getpid()))
    try:
        # oversized output -> notice names an absolute, existing path whose file holds
        # the COMPLETE original output. The marker sits at ~char 20k — inside the
        # omitted middle (head keeps 8k, tail keeps the last 12k of ~40k) — so it is
        # recoverable ONLY through the spill file.
        big = tools.tool_bash(
            "head -c 20000 /dev/zero | tr '\\0' 'x'; printf 'MIDDLE_MARKER'; "
            "head -c 20000 /dev/zero | tr '\\0' 'y'")
        check("spill: notice present", "FULL output saved to " in big, big)
        path = _spill_path_from(big)
        check("spill: path absolute", os.path.isabs(path), path)
        check("spill: file exists", os.path.exists(path), path)
        check("spill: file is 0600", os.stat(path).st_mode & 0o777 == 0o600,
              oct(os.stat(path).st_mode))
        with open(path) as f:
            full = f.read()
        check("spill: marker omitted from transcript", "MIDDLE_MARKER" not in big)
        check("spill: middle recoverable from file", "MIDDLE_MARKER" in full)
        check("spill: file holds the complete output", len(full) == 40013, len(full))

        # output under the cap -> no new spill file
        before = len(os.listdir(session_dir))
        check("spill: small output untouched", tools.tool_bash("printf hi") == "hi")
        check("spill: small output makes no file",
              len(os.listdir(session_dir)) == before, os.listdir(session_dir))

        # killed command with a large partial -> same spill, and the [timed out
        # prefix guardrails.py keys on stays FIRST (the notice lives mid-string).
        killed = tools.tool_bash(
            "head -c 15000 /dev/zero | tr '\\0' 'x'; printf 'KILLED_MID'; "
            "head -c 15000 /dev/zero | tr '\\0' 'y'; sleep 5", timeout=1)
        check("spill: killed keeps prefix", killed.startswith("[timed out after 1s;"),
              killed[:60])
        check("spill: killed gets a path", "FULL output saved to " in killed, killed[:400])
        with open(_spill_path_from(killed)) as f:
            check("spill: killed middle recoverable", "KILLED_MID" in f.read())

        # cap: 25 more oversized results -> only the newest BASH_SPILL_KEEP files remain
        for i in range(25):
            tools._bash_headtail(f"run{i}-" + "z" * tools.BASH_MAX_CHARS)
        names = [n for n in os.listdir(session_dir)
                 if n.startswith("bash-") and n.endswith(".log")]
        check("spill: capped per session", len(names) == tools.BASH_SPILL_KEEP, len(names))

        # stale sibling session dirs get swept on the first spill of a process
        stale = os.path.join(d, "99999999")
        os.makedirs(stale)
        os.utime(stale, (0, 0))
        tools.spill._SWEPT = False
        tools._bash_headtail("s" * (tools.BASH_MAX_CHARS + 1))
        check("spill: stale session swept", not os.path.exists(stale))

        # a failed spill degrades to the old notice, never breaks the result
        os.environ["CHAD_SPILL_DIR"] = "/dev/null/nope"
        deg = tools._bash_headtail("q" * (tools.BASH_MAX_CHARS + 1))
        check("spill: failure falls back", "output truncated" in deg
              and "saved to" not in deg, deg[7990:8100])
    finally:
        if old is None:
            os.environ.pop("CHAD_SPILL_DIR", None)
        else:
            os.environ["CHAD_SPILL_DIR"] = old


def test_bash_headtail_keeps_a_body_that_fits_under_the_cap():
    """A body in the narrow band just below the cap, carrying a clip note. The note is
    metadata ABOUT the body, not part of its budget: counting it sent a body that
    HEAD + TAIL already covers in full down the truncation path, where `omitted` came
    out negative and the two slices overlapped — the middle printed twice, under a
    notice claiming it had been dropped."""
    marker = "MIDDLE_MARKER"
    long_line = "x" * (tools.BASH_LINE_CHARS + 50)          # clipped -> one clip note
    kept = tools.BASH_LINE_CHARS + len("…[line clipped]")
    rows = "\n".join([marker + "y" * (99 - len(marker))] + ["y" * 99] * 200) + "\n"
    body = long_line + "\n" + rows[:tools.BASH_MAX_CHARS - 5 - kept - 1]
    out = tools._bash_headtail(body, spill=False)
    check("headtail: band body is not truncated", "chars omitted" not in out, out[:160])
    check("headtail: band body is not duplicated", out.count(marker) == 1, out.count(marker))
    check("headtail: band body still carries the clip note",
          "chars clipped from over-long lines" in out, out[-140:])


def test_bash_headtail_omits_at_least_one_char_when_it_does_truncate():
    """One char over the cap is the smallest real truncation: head and tail must abut
    without overlapping, and the notice must count the single omitted char."""
    head = ("A" * 99 + "\n") * (tools.BASH_HEAD_CHARS // 100)
    tail = ("Z" * 99 + "\n") * (tools.BASH_TAIL_CHARS // 100)
    out = tools._bash_headtail(head + "M" + tail, spill=False)
    check("headtail: counts one omitted char", "1 chars omitted" in out, out[7990:8120])
    check("headtail: the omitted char is gone", "M" not in out, out[7990:8120])
    check("headtail: head kept whole", out.startswith(head), out[:60])
    check("headtail: tail kept whole", out.endswith(tail), out[-60:])


# --- tool_write ---------------------------------------------------------------

def test_write():
    cwd = os.getcwd()
    try:
        _seed({})
        res = tools.tool_write("sub/dir/x.txt", "hello")
        check("write: [wrote N bytes]", res.startswith("[wrote 5 bytes"), res)
        check("write: created parent dirs + file", os.path.isfile("sub/dir/x.txt"))
        with open("sub/dir/x.txt") as f:
            check("write: file has the content", f.read() == "hello")
    finally:
        os.chdir(cwd)


# --- _under_plans: plan mode's only writable area -------------------------------

def test_under_plans_resolves_symlinks(tmp_path, monkeypatch):
    """A `plans` entry that is a symlink, or a link inside a real `plans`, must not carry
    a plan-mode write out of ./plans/: the check compares real paths."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    os.symlink(tmp_path / "elsewhere", "plans")
    check("symlinked plans dir is rejected", tools._under_plans("plans/x.md") is False)
    check("symlinked plans dir itself is rejected", tools._under_plans("plans") is False)

    os.remove("plans")
    os.mkdir("plans")
    check("real plans dir is accepted", tools._under_plans("plans/x.md") is True)
    check("`..` escape is rejected", tools._under_plans("plans/../x.md") is False)
    os.symlink(tmp_path / "elsewhere" / "x.md", "plans/link.md")
    check("link inside plans pointing out is rejected",
          tools._under_plans("plans/link.md") is False)


# --- outside_workspace: the containment check for in-process writes --------------

def test_outside_workspace_resolves_before_judging(tmp_path, monkeypatch):
    """write/edit never enter the bash seatbelt, so the boundary is this function:
    it judges where a path REALLY lands, not how it is spelled."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.py").write_text("x = 1\n")
    check("a file in the workspace is inside",
          tools.outside_workspace("sub/x.py") is False)
    check("a path that does not exist yet is judged by its parents",
          tools.outside_workspace("new/dir/x.py") is False)
    check("`..` escape is outside", tools.outside_workspace("../x") is True)

    outside = tmp_path.parent / "outside_ws.txt"
    check("an absolute path elsewhere is outside",
          tools.outside_workspace(str(outside)) is True)

    os.symlink(outside, tmp_path / "link.txt")
    check("a symlink inside pointing out is outside",
          tools.outside_workspace("link.txt") is True)

    check(".git/hooks is outside even though it is under the root",
          tools.outside_workspace(".git/hooks/pre-commit") is True)
    check("the rest of .git is not singled out",
          tools.outside_workspace(".git/config") is False)


def test_outside_workspace_takes_an_explicit_root(tmp_path):
    """The root is a parameter so a caller can ask about a directory it is not in."""
    (tmp_path / "in.txt").write_text("")
    check("inside the given root",
          tools.outside_workspace(str(tmp_path / "in.txt"), root=str(tmp_path)) is False)
    check("outside the given root",
          tools.outside_workspace(str(tmp_path.parent / "out.txt"),
                                  root=str(tmp_path)) is True)


# --- write_todos: the wire format ---------------------------------------------
# The tool's format is a markdown checklist — the same text it prints back — because
# that is what a model writes unprompted. Every other shape it has been observed to
# emit still has to land, since a mis-shaped plan used to cost a repair round-trip.

def test_write_todos_formats():
    expected = [("Read the spec", "completed"),
                ("Implement the parser", "in_progress"),
                ("Run the tests", "pending")]
    structured = [{"content": c, "status": s} for c, s in expected]

    def statuses(v):
        return [(t["content"], t["status"]) for t in tools.parse_todos(v)]

    check("todos: structured list", statuses(structured) == expected, statuses(structured))
    # The shape the served model actually emits: the same list as JSON text, because the
    # XML tool-call transport makes every parameter a string.
    check("todos: JSON-string of objects", statuses(json.dumps(structured)) == expected)
    check("todos: single object is a one-item plan",
          statuses({"content": "a", "status": "completed"}) == [("a", "completed")])
    # Status words vary in the wild; none of these should read as anything else.
    check("todos: status synonyms",
          statuses([{"content": "a", "status": "DONE"},
                    {"content": "b", "status": "wip"}])
          == [("a", "completed"), ("b", "in_progress")])
    # An unreadable status must fall to pending — never to finished.
    check("todos: unknown status is pending",
          statuses([{"content": "a", "status": "?"}]) == [("a", "pending")])
    check("todos: missing status is pending",
          statuses([{"content": "a"}]) == [("a", "pending")])
    # Bare strings stay tolerated by the parser (the schema is stricter — see
    # test_write_todos_validation); they are a plan nothing has started.
    check("todos: list of bare strings", statuses(["a", "b"])
          == [("a", "pending"), ("b", "pending")])
    check("todos: empty content dropped",
          statuses([{"content": "  ", "status": "completed"},
                    {"content": "b", "status": "pending"}]) == [("b", "pending")])
    check("todos: unparseable -> nothing",
          statuses("") == [] and statuses(7) == [] and statuses("[x] a checklist") == [])
    # Nothing parseable is a repair message, never a silent empty plan.
    msg = tools.tool_write_todos("")
    check("todos: empty input explains the format",
          msg.startswith("[no todos found") and '"status"' in msg, msg)


def test_write_todos_validation():
    """One shape, so a wrong field is named precisely instead of blurring into
    'some alternative did not match'."""
    from chad.validate import coerce_and_validate

    for label, value in [("structured", [{"content": "a", "status": "pending"}]),
                         ("JSON-string", '[{"content": "a", "status": "pending"}]')]:
        _, errs = coerce_and_validate("write_todos", {"todos": value})
        check(f"todos validate: {label} accepted", not errs, [str(e) for e in errs])

    _, errs = coerce_and_validate("write_todos", {"todos": [{"content": "x", "status": "doing"}]})
    check("todos validate: bad status named precisely",
          any("status" in e.path and "doing" in e.got for e in errs), [str(e) for e in errs])
    _, errs = coerce_and_validate("write_todos", {"todos": ["a", "b"]})
    check("todos validate: bare strings rejected with a field-level error",
          errs and all("content" in e.path or "status" in e.path or "object" in e.expected
                       for e in errs), [str(e) for e in errs])
    _, errs = coerce_and_validate("write_todos", {})
    check("todos validate: missing required reported",
          any(e.got == "missing" for e in errs), [str(e) for e in errs])


# ---------------------------------------------------------------------------
# Read-ish aliases: names the model reaches for that the lean surface does not
# expose. Measured on TB2.1, 4 of 113 trials opened with `read_file` and burned a
# step on the unknown-tool rejection before recovering with bash.
# ---------------------------------------------------------------------------


def test_alias_rewrites_a_read_call_into_the_bash_it_meant():
    assert tools.alias_to_bash("read_file", {"path": "/app/x.py"}) == (
        "bash", {"command": "cat -n /app/x.py"})


def test_alias_quotes_paths_with_spaces():
    name, args = tools.alias_to_bash("view_file", {"filename": "my notes.txt"})
    assert name == "bash" and args["command"] == "cat -n 'my notes.txt'"


def test_alias_honors_offset_and_limit():
    assert tools.alias_to_bash("read", {"file": "a.py", "offset": 10, "limit": 5})[1] == {
        "command": "sed -n '10,14p' a.py | cat -n"}
    assert tools.alias_to_bash("read", {"file": "a.py", "start_line": 3, "end_line": 9})[1] == {
        "command": "sed -n '3,9p' a.py | cat -n"}
    assert tools.alias_to_bash("read", {"file": "a.py", "limit": 20})[1] == {
        "command": "head -n 20 a.py | cat -n"}


def test_alias_leaves_a_pathless_call_alone_for_the_normal_repair_path():
    # Nothing to read -> don't invent a command; let the unknown-tool repair run.
    assert tools.alias_to_bash("read_file", {}) == ("read_file", {})
    assert tools.alias_to_bash("read_file", {"path": "  "}) == ("read_file", {"path": "  "})


def test_alias_never_shadows_a_real_dispatch():
    """An MCP server may legitimately expose `read`; that tool must win over the shim."""
    tools.DISPATCH["read"] = lambda a, ss=None: "from the real tool"
    try:
        assert tools.alias_to_bash("read", {"path": "/a.py"}) == ("read", {"path": "/a.py"})
    finally:
        del tools.DISPATCH["read"]


def test_alias_passes_exposed_tools_through_untouched():
    assert tools.alias_to_bash("bash", {"command": "ls"}) == ("bash", {"command": "ls"})


def test_aliases_are_not_advertised_to_the_model():
    """Accepting a name is free; naming it costs prompt tokens on every request."""
    exposed = {s["function"]["name"] for s in tools.SCHEMAS}
    assert not (exposed & tools.READ_ALIASES)


# ---------------------------------------------------------------------------
# Plan state read by the `done` guardrail.
# ---------------------------------------------------------------------------


def test_unfinished_todos_lists_only_open_items():
    tools.tool_write_todos([{"content": "done one", "status": "completed"},
                            {"content": "doing this", "status": "in_progress"},
                            {"content": "later", "status": "pending"}])
    assert tools.unfinished_todos() == ["doing this", "later"]
    tools.tool_write_todos([{"content": c, "status": "completed"}
                            for c in ("done one", "doing this", "later")])
    assert tools.unfinished_todos() == []
    tools.clear_todos()
    assert tools.unfinished_todos() == []


def test_unfinished_todos_reads_json_text_from_the_xml_transport():
    """The shape this model actually sends, arriving as JSON text over the XML transport."""
    tools.tool_write_todos('[{"content": "a", "status": "completed"}, '
                           '{"content": "b", "status": "pending"}]')
    assert tools.unfinished_todos() == ["b"]
    tools.clear_todos()




_BRIDGE_STUB = """import json, sys
TOOLS = [
 {"name": "bash", "description": "Run.", "inputSchema": {"type": "object", "properties": {"command": {"type": "string"}}}},
 {"name": "write", "description": "Write.", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}},
 {"name": "edit", "description": "Edit.", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}}},
 {"name": "read", "description": "Read.", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}, "annotations": {"readOnlyHint": True}},
]
def send(m):
    sys.stdout.write(json.dumps(m) + "\\n")
    sys.stdout.flush()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    mid = req.get("id")
    method = req.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": req["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "b", "version": "1.0"}}})
    elif method == "notifications/initialized":
        pass
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        name = (req.get("params") or {}).get("name", "")
        send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": "stub-" + name}]}})
    else:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "nope"}})
"""


def _bridge_fixture(tmp_path, monkeypatch):
    from chad import mcp
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    mcp.reset_session()
    return mcp


def _write_bridge_stub(tmp_path):
    path = tmp_path / "bridge_stub.py"
    path.write_text(_BRIDGE_STUB)
    return path


def _overlay_on(mcp, tmp_path, server="bridge"):
    import sys as _sys
    stub = _write_bridge_stub(tmp_path)
    warnings = mcp.set_client_servers(
        str(tmp_path), [(server, {"command": _sys.executable,
                                  "args": [str(stub)]})])
    assert warnings == []
    from chad import tools as _tools
    _tools.set_name_overlay({
        "bash": "mcp__" + server + "__bash",
        "write": "mcp__" + server + "__write",
        "edit": "mcp__" + server + "__edit",
        "read": "mcp__" + server + "__read",
    })


def _overlay_off(tmp_path):
    from chad import mcp as _mcp
    from chad import tools as _tools
    _tools.set_name_overlay({})
    _mcp.set_client_servers(str(tmp_path), [])
    _mcp.reset_session()


def test_overlay_renames_and_hides(tmp_path, monkeypatch):
    from chad import tools as _tools
    mcp = _bridge_fixture(tmp_path, monkeypatch)
    try:
        _overlay_on(mcp, tmp_path)
        names = [s["function"]["name"] for s in _tools.active_schemas()]
        assert "bash" in names and "write" in names
        assert "edit" in names and "read" in names
        assert "mcp__bridge__bash" not in names
        params = [s for s in _tools.active_schemas()
                  if s["function"]["name"] == "bash"][0]["function"]["parameters"]
        assert params["properties"]["command"]["type"] == "string"
    finally:
        _overlay_off(tmp_path)


def test_overlay_dispatch_and_mutating(tmp_path, monkeypatch):
    from chad import tools as _tools
    mcp = _bridge_fixture(tmp_path, monkeypatch)
    try:
        _overlay_on(mcp, tmp_path)
        assert _tools.resolve_overlay("bash") == "mcp__bridge__bash"
        assert _tools.resolve_overlay("done") == "done"
        assert _tools.is_overlay("edit") is True
        assert _tools.is_overlay("bash") is True
        assert _tools.is_mutating("bash") is True
        assert _tools.is_mutating("read") is False
        fn = _tools.dispatch_for("bash")
        assert fn is not None
        assert fn({"command": "ls"}) == "stub-bash"
        assert _tools.dispatch_for("read")({"path": "x"}) == "stub-read"
    finally:
        _overlay_off(tmp_path)


def test_overlay_missing_target_fails_closed(tmp_path, monkeypatch):
    from chad import tools as _tools
    _bridge_fixture(tmp_path, monkeypatch)
    try:
        _tools.set_name_overlay({"bash": "mcp__ghost__bash"})
        names = [s["function"]["name"] for s in _tools.active_schemas()]
        assert "bash" not in names
        assert _tools.dispatch_for("bash") is None
    finally:
        _overlay_off(tmp_path)


def test_alias_prefers_overlay_read(tmp_path, monkeypatch):
    from chad import tools as _tools
    mcp = _bridge_fixture(tmp_path, monkeypatch)
    try:
        _overlay_on(mcp, tmp_path)
        assert _tools.alias_to_bash("view", {"path": "a.py"}) == (
            "read", {"path": "a.py"})
        assert _tools.alias_to_bash(
            "view", {"path": "a.py", "offset": 3, "limit": 5}) == (
            "read", {"path": "a.py", "offset": 3, "limit": 5})
    finally:
        _overlay_off(tmp_path)
