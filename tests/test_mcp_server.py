"""Unit tests for aimux.mcp_server (stdio MCP server, codex-facing tools)."""
import base64
import re

import pytest

from aimux import mcp_server


class ExecClient:
    def __init__(self):
        self.sent = []
        self.captures = []
        self.killed = []

    def create_session(self, **kwargs):
        return {"ok": True}

    def send_keys(self, **kwargs):
        self.sent.append(kwargs)
        return {"ok": True}

    def capture(self, **kwargs):
        return self.captures.pop(0) if self.captures else {"content": ""}

    def kill_session(self, **kwargs):
        self.killed.append(kwargs)
        return {"ok": True}


class FakeClient:
    """Stand-in for BridgeControlClient; keeps an in-memory remote filesystem."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.health_called = False

    def health(self, *, timeout=2.0):
        self.health_called = True
        return {"ok": True}

    def get_remote(self, name, *, timeout=2.0):
        return {"name": name, "default_profile": "posix-pty", "status": "connected"}

    def file_read(self, *, remote, path, timeout=180.0):
        if path not in self.files:
            raise RuntimeError(f"not found: {path}")
        return {"content_b64": base64.b64encode(self.files[path]).decode()}

    def file_write(self, *, remote, path, content_b64, mode=None, max_size=None, timeout=180.0):
        self.files[path] = base64.b64decode(content_b64)
        return {"ok": True}

    def file_delete(self, *, remote, path, recursive=False, timeout=60.0):
        self.files.pop(path, None)
        return {"ok": True}


@pytest.fixture
def fake(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    return c


# ---- patch parser & applier ----

def test_parse_add_update_delete():
    ops = mcp_server._parse_codex_patch(
        "*** Begin Patch\n"
        "*** Add File: a.txt\n+hello\n"
        "*** Update File: b.txt\n@@\n-old\n+new\n"
        "*** Delete File: c.txt\n*** End Patch\n"
    )
    assert len(ops) == 3
    assert ops[0] == {"kind": "add", "path": "a.txt", "content": "hello"}
    assert ops[1]["kind"] == "update" and ops[1]["path"] == "b.txt"
    assert ops[2] == {"kind": "delete", "path": "c.txt"}


def test_apply_hunks_simple():
    new = mcp_server._apply_hunks("foo\nold\nbar\n", [{"lines": ["@@", "-old", "+new"]}])
    assert new == "foo\nnew\nbar\n"


def test_apply_hunks_with_context():
    new = mcp_server._apply_hunks("a\nb\nc\nd\n", [{"lines": ["@@", " b", "-c", "+C", " d"]}])
    assert new == "a\nb\nC\nd\n"


def test_apply_hunks_rejects_missing_context():
    with pytest.raises(ValueError, match="patch context not found"):
        mcp_server._apply_hunks("a\nb\n", [{"lines": ["@@", "-missing", "+new"]}])


def test_parse_rejects_missing_begin_marker():
    # The exact "silent {}" failure: an op line without the Begin/End wrapper
    # must raise a self-explanatory error, not parse to zero ops.
    with pytest.raises(ValueError, match="Begin Patch"):
        mcp_server._parse_codex_patch("*** Add File: patch_probe_rel.txt\nrelative path test\n")


def test_parse_rejects_missing_end_marker():
    with pytest.raises(ValueError, match="End Patch"):
        mcp_server._parse_codex_patch("*** Begin Patch\n*** Add File: /x\n+hello\n")


def test_parse_rejects_no_operations():
    with pytest.raises(ValueError, match="no file operations"):
        mcp_server._parse_codex_patch("*** Begin Patch\n*** End Patch\n")


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ("*** Add File:", "missing a path"),
        ("*** Update File:", "missing a path"),
        ("*** Delete File", "malformed Delete File"),
    ],
)
def test_parse_rejects_malformed_operation_headers(header, message):
    with pytest.raises(ValueError, match=message):
        mcp_server._parse_codex_patch(
            f"*** Begin Patch\n{header}\n*** End Patch\n"
        )


def test_parse_rejects_update_without_hunk():
    with pytest.raises(ValueError, match="contains no hunks"):
        mcp_server._parse_codex_patch(
            "*** Begin Patch\n*** Update File: /tmp/file\n-old\n+new\n*** End Patch\n"
        )


def test_parse_rejects_update_without_edits():
    with pytest.raises(ValueError, match="contains no edits"):
        mcp_server._parse_codex_patch(
            "*** Begin Patch\n*** Update File: /tmp/file\n@@\n context\n*** End Patch\n"
        )


def test_parse_rejects_unknown_marker():
    with pytest.raises(ValueError, match="unsupported patch marker"):
        mcp_server._parse_codex_patch(
            "*** Begin Patch\n*** Rename File: /tmp/file\n*** End Patch\n"
        )


# ---- tools ----

def test_view_image(fake):
    fake.files["/x.png"] = b"\x89PNG\r\n fake image bytes"
    r = mcp_server._remote_view_image("r", "p", {"path": "/x.png"})
    assert r["detail"] == "high"
    assert r["image_url"].startswith("data:image/png;base64,")
    assert base64.b64decode(r["image_url"].split(",", 1)[1]) == fake.files["/x.png"]


def test_view_image_jpeg_and_detail(fake):
    fake.files["/y.JPG"] = b"jpeg bytes"
    r = mcp_server._remote_view_image("r", "p", {"path": "/y.JPG", "detail": "original"})
    assert r["detail"] == "original"
    assert r["image_url"].startswith("data:image/jpeg;base64,")


def test_ping(fake):
    assert mcp_server._remote_ping("r", "p", {}) == {"ok": True}
    assert fake.health_called


def test_write_stdin_control_key_is_raw(monkeypatch):
    c = ExecClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()
    sid = mcp_server._create_exec_session("r", "posix-pty", "less file", None)
    c.captures = [{"content": ""}]

    result = mcp_server._remote_write_stdin("r", "posix-pty", {"session_id": sid, "chars": "q", "yield_time_ms": 250})

    assert c.sent[0]["keys"] == ["q"]
    assert result["session_id"] == sid


def test_write_stdin_command_keeps_implicit_enter_and_supports_override(monkeypatch):
    c = ExecClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()
    sid = mcp_server._create_exec_session("r", "posix-pty", "bash", None)
    c.captures = [{"content": ""}, {"content": ""}]

    mcp_server._remote_write_stdin("r", "posix-pty", {"session_id": sid, "chars": "echo ready", "yield_time_ms": 250})
    assert c.sent[0]["keys"] == ["echo ready", "Enter"]

    c.sent.clear()
    c.captures = [{"content": ""}]
    mcp_server._remote_write_stdin("r", "posix-pty", {"session_id": sid, "chars": "raw", "append_enter": False, "yield_time_ms": 250})
    assert c.sent[0]["keys"] == ["raw"]


def test_exec_marker_is_chained_after_command_not_sent_as_followup_stdin():
    posix = mcp_server._command_with_exit_marker("python3 -c 'read(1)'", "__AIMUX_EXIT_ab12__", "posix-pty")
    assert posix == "python3 -c 'read(1)'; echo __AIMUX_EXIT_ab12__:$?"

    powershell = mcp_server._command_with_exit_marker("Read-Host", "__AIMUX_EXIT_ab12__", "powershell")
    assert powershell == "& { Read-Host }; echo __AIMUX_EXIT_ab12__$LASTEXITCODE"

    cmd = mcp_server._command_with_exit_marker("type nul", "__AIMUX_EXIT_ab12__", "cmd")
    # Caret-escaped: '%%errorlevel%%' is the batch-file spelling and, typed at an
    # interactive prompt, CALL turns it into a literal '%0%' — a marker whose
    # digits never arrive, so the session never looked finished.
    assert cmd == "type nul & call echo __AIMUX_EXIT_ab12__^%errorlevel^%"


def test_apply_patch_end_to_end(fake):
    fake.files["/f"] = b"foo\nold\nbar\n"
    r = mcp_server._remote_apply_patch(
        "r", "p",
        {"input": "*** Begin Patch\n*** Update File: /f\n@@\n-old\n+new\n*** End Patch\n"},
    )
    assert r["applied"] == ["updated: /f"]
    assert fake.files["/f"] == b"foo\nnew\nbar\n"


def test_apply_patch_add_and_delete(fake):
    r = mcp_server._remote_apply_patch(
        "r", "p",
        {"input": "*** Begin Patch\n*** Add File: /n\n+hello world\n*** Delete File: /g\n*** End Patch\n"},
    )
    assert r["applied"] == ["added: /n", "deleted: /g"]
    assert fake.files["/n"] == b"hello world"
    assert "/g" not in fake.files


def test_apply_patch_relative_path_rejected(fake):
    with pytest.raises(ValueError, match="must be absolute"):
        mcp_server._remote_apply_patch(
            "r", "p",
            {"input": "*** Begin Patch\n*** Add File: src/conv.py\n+hello\n*** End Patch\n"},
        )


def test_apply_patch_bad_format_raises_not_silent_empty(fake):
    # Regression guard: a malformed patch must surface as an error, never as a
    # successful `{"applied": []}` that makes the agent think the tool is broken.
    with pytest.raises(ValueError, match="Begin Patch"):
        mcp_server._remote_apply_patch(
            "r", "p",
            {"input": "*** Add File: /cpfs01/user/x/patch_probe.txt\nrelative path test\n"},
        )


def test_apply_patch_windows_absolute_path_accepted(fake):
    result = mcp_server._remote_apply_patch(
        "r", "p",
        {"input": "*** Begin Patch\n*** Add File: C:\\proj\\src\\conv.py\n+hello\n*** End Patch\n"},
    )
    assert result["applied"] == ["added: C:\\proj\\src\\conv.py"]
    assert fake.files["C:\\proj\\src\\conv.py"] == b"hello"


# ---- JSON-RPC dispatch ----

def test_jsonrpc_initialize(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert sent and sent[0]["result"]["protocolVersion"]
    assert "tools" in sent[0]["result"]["capabilities"]


def test_jsonrpc_tools_list(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in sent[0]["result"]["tools"]]
    assert names == ["remote_exec_command", "remote_write_stdin", "remote_apply_patch", "remote_view_image", "remote_download_file", "remote_ping"]


def test_jsonrpc_tools_call_ping(monkeypatch, fake):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "remote_ping", "arguments": {}},
    })
    assert sent[0]["result"]["content"][0]["text"] == '{"ok": true}'


def test_jsonrpc_unknown_tool(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    })
    assert sent[0]["error"]["code"] == -32601


def test_jsonrpc_apply_patch_format_error_is_visible(monkeypatch, fake):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {
            "name": "remote_apply_patch",
            "arguments": {"input": "*** Add File: /tmp/file\nhello\n"},
        },
    })
    result = sent[0]["result"]
    assert result["isError"] is True
    assert "Begin Patch" in result["content"][0]["text"]


def test_jsonrpc_initialized_notification_no_response(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert sent == []  # notifications get no response


# ---- temp-file exec (POSIX) ----


class ExecTempClient:
    """Session ops + in-memory remote FS for the temp-file exec path."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.writes: list[tuple[str, bytes]] = []
        self.sent: list[list[str]] = []
        self.killed: list[dict] = []

    def create_session(self, **kw):
        return {"ok": True}

    def kill_session(self, **kw):
        self.killed.append(kw)
        return {"ok": True}

    def send_keys(self, *, remote, name, keys, flags, timeout=30.0):
        self.sent.append(list(keys))
        # keys[0] is the launch line, keys[1:] is "Enter". Simulate the remote
        # shell running it: exit 0, output = the command we wrote to the script.
        launch = str(keys[0])
        m = re.match(r"(\S+) (\S+) > (\S+) 2>&1; echo \$\? > (\S+)", launch)
        assert m, f"unexpected launch line: {launch!r}"
        _, script, out, code = m.groups()
        self.files[code] = b"0"
        self.files[out] = self.files.get(script, b"") + b"\n"
        return {"ok": True}

    def file_write(self, *, remote, path, content_b64, mode=None, max_size=None, timeout=180.0):
        data = base64.b64decode(content_b64)
        self.files[path] = data
        self.writes.append((path, data))
        return {"ok": True}

    def file_stat(self, *, remote, path, timeout=60.0):
        if path in self.files:
            return {"exists": True, "type": "file", "size": len(self.files[path])}
        return {"exists": False, "type": "none", "size": 0}

    def file_read(self, *, remote, path, timeout=180.0):
        return {"content_b64": base64.b64encode(self.files[path]).decode()}

    def file_delete(self, *, remote, path, recursive=False, timeout=60.0):
        self.files.pop(path, None)
        return {"ok": True}


def test_exec_temp_base_naming():
    base = mcp_server._exec_temp_base()
    assert re.match(r"^incoming_command_\d{8}_\d{6}_\d{6}$", base)


def test_exec_temp_paths_same_base():
    s, o, c = mcp_server._exec_temp_paths()
    assert s.startswith("/tmp/incoming_command_") and s.endswith(".sh")
    base = s[:-3]  # strip the .sh extension
    assert o == base + ".out"
    assert c == base + ".code"


def test_exec_tempfile_writes_script_runs_and_cleans_up(monkeypatch):
    c = ExecTempClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()

    r = mcp_server._remote_exec_command(
        "r", "posix-pty", {"cmd": "echo hello\npwd", "yield_time_ms": 1000}
    )

    assert r["exit_code"] == 0
    assert r["output"] == "echo hello\npwd\n"
    # command was written verbatim to the script file
    assert any(path.endswith(".sh") and data == b"echo hello\npwd" for path, data in c.writes)
    # launch line ran the script and redirected output + exit code
    assert len(c.sent) == 1
    assert c.sent[0][0].startswith("bash /tmp/incoming_command_")
    # finished -> session killed + all three temp files cleaned up
    assert c.killed
    assert c.files == {}


def test_exec_tempfile_still_running_returns_session_id(monkeypatch):
    class StuckClient(ExecTempClient):
        def send_keys(self, **kw):
            self.sent.append(list(kw["keys"]))
            return {"ok": True}

    c = StuckClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()

    r = mcp_server._remote_exec_command(
        "r", "posix-pty", {"cmd": "sleep 100", "yield_time_ms": 300}
    )

    assert "session_id" in r
    assert "exit_code" not in r  # still running -> no exit code yet
    assert r["session_id"] in mcp_server._sessions  # retained for later poll


def test_exec_windows_profile_uses_temp_script_not_marker(monkeypatch):
    # Windows no longer goes through the PTY + exit-marker path: cmd could not
    # report its exit code at an interactive prompt, so those execs never
    # finished. It uses the same temp-script mechanism as every other platform.
    c = WindowsExecClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()

    r = mcp_server._remote_exec_command("r", "cmd", {"cmd": "dir", "yield_time_ms": 250})

    assert "exit_code" in r
    keys = c.sent[0]
    assert keys[1] == "Enter"
    assert "echo __AIMUX_EXIT_" not in keys[0]  # no marker suffix
    assert keys[0].startswith('"%USERPROFILE%\\AppData\\Local\\Temp\\')


def test_exec_interactive_true_uses_pty_path(monkeypatch):
    # interactive=true must keep the live-PTY marker path (for write_stdin),
    # not the one-shot temp-script path.
    c = ExecClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()
    c.captures = [{"content": ""}]

    mcp_server._remote_exec_command(
        "r", "posix-pty", {"cmd": "python3 -i", "interactive": True, "yield_time_ms": 250}
    )

    assert c.sent
    keys = c.sent[0]["keys"]
    assert "echo __AIMUX_EXIT_" in keys[0]  # marker path, not a temp-file launch
    assert not keys[0].startswith("bash /tmp/incoming_command_")


def test_apply_hunks_whitespace_mismatch_reports_specific_error():
    # File has "# ---" at column 0; the patch context wrote " # ---" (one extra
    # leading space). The error must name the whitespace mismatch, not just say
    # "context not found".
    orig = "# ---\ndef foo():\n    pass\n"
    hunks = [{"lines": ["@@", "  # ---", "+x"]}]
    with pytest.raises(ValueError) as exc:
        mcp_server._apply_hunks(orig, hunks)
    msg = str(exc.value)
    assert "whitespace mismatch" in msg
    assert "' # ---'" in msg  # the patch's (wrong) context line
    assert "'# ---'" in msg   # the file's (correct) line


def test_apply_hunks_skips_whitespace_diag_on_huge_file(monkeypatch):
    # Above the size threshold the O(file) whitespace scan is skipped; the
    # error falls back to the generic message.
    monkeypatch.setattr(mcp_server, "_WS_DIAG_MAX_BYTES", 10)
    orig = "# ---\n" + ("x" * 100) + "\n"  # > 10 bytes
    hunks = [{"lines": ["@@", "  # ---", "+y"]}]
    with pytest.raises(ValueError) as exc:
        mcp_server._apply_hunks(orig, hunks)
    assert str(exc.value) == "patch context not found"


# ---- temp-file exec (Windows) ----
#
# Windows used to be routed to the live-PTY path, where completion was detected
# by scraping an exit marker out of the terminal. At an interactive cmd prompt
# the marker's ``%%errorlevel%%`` expanded to a literal ``%0%``, so the reader
# never saw the digits it matches on and every cmd exec hung forever on
# session_id. All platforms now use the temp-script path.


def _win_norm(path: str) -> str:
    """``%USERPROFILE%\\AppData\\...`` → ``~/AppData/...`` (the client's view)."""
    return path.replace("\\", "/").replace("%USERPROFILE%", "~")


class WindowsExecClient(ExecTempClient):
    """Fake Windows client: honours the cmd launcher and its %USERPROFILE% paths."""

    def send_keys(self, *, remote, name, keys, flags, timeout=30.0):
        self.sent.append(list(keys))
        launch = str(keys[0])
        m = re.match(r'^"([^"]+)" > "([^"]+)" 2>&1$', launch)
        assert m, f"unexpected windows launch line: {launch!r}"
        script, out = (_win_norm(m.group(1)), _win_norm(m.group(2)))
        # The launcher speaks %USERPROFILE% while the MCP wrote the script through
        # '~' — the mapping only holds if both name the same remote file.
        assert script in self.files, f"launcher pointed at a script that was never written: {script}"
        self.files[out] = b"winbox\\user\r\n"  # the batch file's stdout
        self.files[out[:-4] + ".code"] = b"3\r\n"  # its 'echo %errorlevel%' line
        return {"ok": True}


def test_exec_plan_posix_unchanged():
    plan = mcp_server._exec_plan(cmd="echo hi", profile="posix-pty", shell_bin=None, windows=False)
    assert plan.script.startswith("/tmp/incoming_command_") and plan.script.endswith(".sh")
    assert plan.body == "echo hi"
    assert plan.launch == f"bash {plan.script} > {plan.out} 2>&1; echo $? > {plan.code}"


def test_exec_plan_windows_cmd_writes_exit_code_inside_the_batch_file():
    plan = mcp_server._exec_plan(cmd="whoami", profile="cmd", shell_bin=None, windows=True)
    assert plan.script.startswith("~/AppData/Local/Temp/") and plan.script.endswith(".cmd")
    assert plan.body.startswith("@echo off\r\nwhoami\r\n")
    assert f'echo %errorlevel% > "{mcp_server._win_path(plan.code)}"' in plan.body
    assert plan.launch == f'"{mcp_server._win_path(plan.script)}" > "{mcp_server._win_path(plan.out)}" 2>&1'


def test_exec_plan_windows_powershell_captures_output_itself():
    plan = mcp_server._exec_plan(cmd="Get-Date", profile="powershell", shell_bin=None, windows=True)
    assert plan.script.endswith(".ps1")
    # A PS '>' writes UTF-16, so the script must write the files with .NET.
    assert "[IO.File]::WriteAllText(" in plan.body
    assert "$LASTEXITCODE" in plan.body
    assert plan.launch.startswith("powershell -NoProfile -ExecutionPolicy Bypass -File ")


def test_exec_plan_windows_mingw_uses_home_paths():
    plan = mcp_server._exec_plan(cmd="ls", profile="mingw64", shell_bin=None, windows=True)
    assert plan.script.startswith("~/AppData/Local/Temp/") and plan.script.endswith(".sh")
    # Git Bash cannot read the bridge client's '~' spelling, and $HOME inside
    # double quotes is the one form that expands there.
    assert plan.launch.startswith('bash "$HOME/AppData/Local/Temp/')
    assert plan.body == "ls"


def test_exec_temp_paths_windows_extensions():
    s, o, c = mcp_server._exec_temp_paths("cmd", windows=True)
    assert s.endswith(".cmd") and o.endswith(".out") and c.endswith(".code")
    assert s[:-4] == o[:-4] == c[:-5]
    assert s.startswith("~/AppData/Local/Temp/")


def test_windows_exec_runs_cleans_up_and_reports_exit_code(monkeypatch):
    c = WindowsExecClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()

    r = mcp_server._remote_exec_command("r", "cmd", {"cmd": "whoami", "yield_time_ms": 1000})

    assert r["exit_code"] == 3
    assert r["output"] == "winbox\\user\r\n"  # the command's output, nothing else
    assert c.killed and c.files == {}  # session + all three temp files reaped


def test_windows_exec_reports_platform_even_for_posix_family_profile(monkeypatch):
    """A MSYS profile on a Windows host still needs Windows temp paths."""
    monkeypatch.setattr(mcp_server, "_remote_platform", "windows")
    c = WindowsExecClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()
    plan_cmds = []

    real_plan = mcp_server._exec_plan

    def spy(**kw):
        plan_cmds.append(kw)
        return real_plan(**kw)

    monkeypatch.setattr(mcp_server, "_exec_plan", spy)
    mcp_server._remote_exec_command("r", "cmd", {"cmd": "whoami", "yield_time_ms": 1000})
    assert plan_cmds and plan_cmds[0]["windows"] is True


def test_remote_is_windows_falls_back_to_profile_names():
    mcp_server._remote_platform = ""
    assert mcp_server._remote_is_windows("cmd")
    assert mcp_server._remote_is_windows("powershell")
    assert mcp_server._remote_is_windows("mingw64")
    assert not mcp_server._remote_is_windows("posix-pty")


def test_decode_text_tolerates_windows_shell_encodings():
    assert mcp_server._decode_text("hi".encode("utf-8")) == "hi"
    assert mcp_server._decode_text("﻿hi".encode("utf-8")) == "hi"          # UTF-8 BOM
    assert mcp_server._decode_text("hi".encode("utf-16")) == "hi"              # UTF-16 BOM
    assert mcp_server._decode_text("保留".encode("utf-8")) == "保留"


def test_read_code_file_ignores_noise_around_the_number(monkeypatch):
    class C:
        def __init__(self, data):
            self.data = data

        def file_stat(self, **kw):
            return {"exists": True, "type": "file", "size": len(self.data)}

        def file_read(self, **kw):
            return {"content_b64": base64.b64encode(self.data).decode()}

    for raw, expected in ((b"0\r\n", 0), ("﻿7".encode("utf-8"), 7),
                          ("8".encode("utf-16"), 8), (b"exit=13 ok", 13), (b"", None)):
        assert mcp_server._read_code_file(C(raw), "r", "/tmp/x.code") == expected
