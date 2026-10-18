"""Unit tests for aimux.mcp_server (stdio MCP server, codex-facing tools)."""
import base64
import json
import os
import re
import time

import pytest
from pathlib import Path

from aimux import bridge
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
        self.system_info_called = False
        # Flip to make the live system report fail, the way an old or
        # disconnected reverse client does.
        self.system_info_error = ""

    def health(self, *, timeout=2.0):
        self.health_called = True
        return {"ok": True, "version": "0.3.72"}

    def get_remote(self, name, *, timeout=2.0):
        return {
            "name": name,
            "default_profile": "posix-pty",
            "status": "connected",
            "platform": "windows",
            "client_version": "0.3.70",
            "profiles": [{"name": "cmd", "available": True}],
        }

    def system_info(self, *, remote, timeout=15.0):
        self.system_info_called = True
        if self.system_info_error:
            raise RuntimeError(self.system_info_error)
        return {
            "os": "darwin",
            "os_name": "Darwin",
            "os_release": "23.6.0",
            "os_version": "Darwin Kernel Version 23.6.0",
            "os_detail": "macOS-14.6-arm64-arm-64bit",
            "arch": "arm64",
            "hostname": "macbox",
            "user": "ada",
            "home": "/Users/ada",
            "cwd": "/Users/ada/work",
            "path_style": "posix",
            "path_separator": "/",
            "default_profile": "mac-pty",
            "shells": [{"name": "mac-pty", "available": True}],
            "env": {"SHELL": "/bin/zsh"},
            "encoding": "UTF-8",
            "python": "3.12.7",
            "aimux_version": "0.3.72",
            "gui": {"available": False, "error": "gui not requested"},
            "time": "2026-10-16T10:00:00+0800",
            "timezone": "CST",
            # Not on the reported whitelist: the ping must drop it.
            "internal_bookkeeping": "drop me",
        }

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
    r = mcp_server._remote_ping("r", "p", {})
    assert r["ok"] is True and fake.health_called and fake.system_info_called
    assert r["broker"] == {"ok": True, "version": "0.3.72"}
    assert r["live"] is True
    assert r["system"]["os"] == "darwin"
    assert r["system"]["hostname"] == "macbox"
    assert r["system"]["user"] == "ada"
    # The whitelist drops whatever else the client happened to send.
    assert "internal_bookkeeping" not in r["system"]
    assert r["session"]["profile"] == "p"
    assert r["summary"].startswith("remote r: Darwin 23.6.0 arm64")
    assert "macbox" in r["summary"]


def test_ping_reports_workdir_with_its_source(fake, monkeypatch):
    monkeypatch.setattr(mcp_server, "_effective_workdir", lambda args, remote: ("/srv/app", "session_default"))
    r = mcp_server._remote_ping("r", "p", {})
    assert r["session"]["workdir"] == "/srv/app"
    assert r["session"]["workdir_source"] == "session_default"
    assert "/srv/app" in r["summary"]
    assert "notes" not in r


def test_ping_tells_windows_shell_syntax(fake, monkeypatch):
    # run_stdio() learns the platform from the registration; unit tests set it.
    monkeypatch.setattr(mcp_server, "_remote_platform", "windows")
    r = mcp_server._remote_ping("r", "mingw64", {})
    assert r["session"]["remote_is_windows"] is True
    assert r["session"]["shell_family"] == "posix"
    assert "Git-Bash" in r["session"]["shell_syntax"]


def test_ping_notes_missing_workdir(fake):
    r = mcp_server._remote_ping("r", "p", {})
    assert r["session"]["workdir"] is None
    assert r["session"]["workdir_source"] is None
    # Where commands start is only knowable from the live report.
    assert "/Users/ada/work" in r["notes"][0]


def test_ping_falls_back_to_the_registry(fake, monkeypatch):
    """An old or disconnected client still gets described, not left blank."""
    fake.system_info_error = "BridgeError: unsupported bridge message type 'sys.info'"
    r = mcp_server._remote_ping("r", "p", {})
    assert r["ok"] is True
    assert r["live"] is False
    assert r["system"] == {"os": "windows", "aimux_version": "0.3.70"}
    assert r["shells"] == [{"name": "cmd", "available": True}]
    assert "unsupported bridge message type" in r["notes"][0]
    assert "did not answer a live probe" in r["summary"]


def test_ping_reports_broker_failure(monkeypatch, fake):
    def boom(*, timeout=2.0):
        raise OSError("connection refused")
    monkeypatch.setattr(fake, "health", boom)
    r = mcp_server._remote_ping("r", "p", {})
    assert r["ok"] is False
    assert "connection refused" in r["broker"]["error"]


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


def test_apply_patch_context_mismatch_points_at_remote_exec_command(fake):
    # Retrying an unmatched hunk can never succeed, so the error must name the
    # recovery step: read the file again with remote_exec_command.
    fake.files["/f"] = b"foo\nold\nbar\n"
    with pytest.raises(ValueError) as exc:
        mcp_server._remote_apply_patch(
            "r", "p",
            {"input": "*** Begin Patch\n*** Update File: /f\n@@\n-nope\n+x\n*** End Patch\n"},
        )
    assert str(exc.value) == (
        "patch context not found, you should read this file again with remote_exec_command!"
    )
    assert fake.files["/f"] == b"foo\nold\nbar\n"  # nothing written


def test_apply_patch_whitespace_mismatch_keeps_diagnosis_and_hint(fake):
    fake.files["/f"] = b"# ---\ndef foo():\n    pass\n"
    with pytest.raises(ValueError) as exc:
        mcp_server._remote_apply_patch(
            "r", "p",
            {"input": "*** Begin Patch\n*** Update File: /f\n@@\n  # ---\n+x\n*** End Patch\n"},
        )
    msg = str(exc.value)
    assert "whitespace mismatch on context line 1" in msg
    assert msg.endswith(
        "You should read this file again with remote_exec_command and retry with the exact context."
    )


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
    assert names == ["remote_exec_command", "remote_set_remote_workdir", "remote_write_stdin",
                    "remote_apply_patch", "remote_view_image", "remote_download_file", "remote_ping"]


def test_jsonrpc_tools_call_ping(monkeypatch, fake):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "remote_ping", "arguments": {}},
    })
    payload = json.loads(sent[0]["result"]["content"][0]["text"])
    assert payload["ok"] is True
    assert payload["system"]["os"] == "darwin"
    assert payload["summary"].startswith("remote rem:")


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


def test_jsonrpc_apply_patch_context_mismatch_is_actionable(monkeypatch, fake):
    # The text an agent actually sees: the dispatcher prefixes the exception
    # type, so assert the whole rendered message rather than just the hint.
    fake.files["/f"] = b"foo\nold\nbar\n"
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {
            "name": "remote_apply_patch",
            "arguments": {"input": "*** Begin Patch\n*** Update File: /f\n@@\n-nope\n+x\n*** End Patch\n"},
        },
    })
    result = sent[0]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "ValueError: patch context not found, "
        "you should read this file again with remote_exec_command!"
    )


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
    assert s.startswith("/tmp/aimux_tmp/incoming_command_") and s.endswith(".sh")
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
    assert c.sent[0][0].startswith("bash /tmp/aimux_tmp/incoming_command_")
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
    assert not keys[0].startswith("bash /tmp/aimux_tmp/incoming_command_")


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
    assert plan.script.startswith("/tmp/aimux_tmp/incoming_command_") and plan.script.endswith(".sh")
    assert plan.body == "echo hi"
    assert plan.launch == f"bash {plan.script} > {plan.out} 2>&1; echo $? > {plan.code}"


def test_exec_plan_windows_cmd_writes_exit_code_inside_the_batch_file():
    plan = mcp_server._exec_plan(cmd="whoami", profile="cmd", shell_bin=None, windows=True)
    assert plan.script.startswith("~/AppData/Local/Temp/aimux_tmp/") and plan.script.endswith(".cmd")
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
    assert plan.script.startswith("~/AppData/Local/Temp/aimux_tmp/") and plan.script.endswith(".sh")
    # Git Bash cannot read the bridge client's '~' spelling, and $HOME inside
    # double quotes is the one form that expands there.
    assert plan.launch.startswith('bash "$HOME/AppData/Local/Temp/aimux_tmp/')
    assert plan.body == "ls"


def test_exec_temp_paths_windows_extensions():
    s, o, c = mcp_server._exec_temp_paths("cmd", windows=True)
    assert s.endswith(".cmd") and o.endswith(".out") and c.endswith(".code")
    assert s[:-4] == o[:-4] == c[:-5]
    assert s.startswith("~/AppData/Local/Temp/aimux_tmp/")


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


# ---- dead-session detection + temp-file cleanup on reap ----


class DeadSessionClient(ExecTempClient):
    """Session disappears mid-exec: no code file ever lands, capture says gone."""

    def __init__(self):
        super().__init__()
        self.captures: list[str] = []

    def send_keys(self, **kw):  # the launch never lands: session died first
        self.sent.append(list(kw.get("keys") or []))
        return {"ok": True}

    def capture(self, **kw):
        self.captures.append(str(kw.get("name")))
        raise bridge.BridgeError("session not found", kind="session_gone")


def test_wait_exec_done_detects_a_session_that_vanished(monkeypatch):
    # A killed session leaves its temp files behind, so the code file never
    # appears: without a liveness probe this loops until the yield expires and
    # then reports "still running" for a session nobody can ever finish.
    c = DeadSessionClient()
    out, exit_code, finished, wall, dead = mcp_server._wait_exec_done(
        c, "r", "/tmp/x.out", "/tmp/x.code", 3000, name="mcp-exec-abc", probe_every=0.4
    )
    assert (finished, dead, exit_code) == (True, True, -1)
    assert c.captures, "the wait must probe the session, not only the files"


def test_session_is_gone_only_believes_session_errors():
    class C:
        def __init__(self, exc):
            self.exc = exc

        def capture(self, **kw):
            if self.exc:
                raise self.exc
            return {"content": "x"}

    assert mcp_server._session_is_gone(C(bridge.BridgeError("g", kind="session_gone")), "r", "n") is True
    assert mcp_server._session_is_gone(C(bridge.BridgeError("g", kind="not_found")), "r", "n") is True
    # A transport hiccup is not proof of death — keep waiting.
    assert mcp_server._session_is_gone(C(RuntimeError("boom")), "r", "n") is False
    assert mcp_server._session_is_gone(C(bridge.BridgeError("other", kind="bad_request")), "r", "n") is False
    assert mcp_server._session_is_gone(C(None), "r", "n") is False


def test_exec_reports_dead_session_and_cleans_up(monkeypatch):
    monkeypatch.setattr(mcp_server, "_EXEC_PROBE_EVERY_S", 0.4)
    c = DeadSessionClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()

    r = mcp_server._remote_exec_command("r", "posix-pty", {"cmd": "sleep 900", "yield_time_ms": 3000})

    assert r["exit_code"] == -1
    assert r["note"] == "session ended (shell exited)"
    assert r["output"] == "<aimux>session ended (shell exited)</aimux>"
    assert "session_id" not in r
    assert c.killed and c.files == {}  # session + all three temp files cleaned up


def test_reap_deletes_temp_files_of_unfinished_sessions(monkeypatch):
    # Bug: reaping only killed the session, leaving the script/out/code files on
    # the remote (and on Windows, in the user's AppData\Local\Temp) forever.
    c = ExecTempClient()
    mcp_server._sessions.clear()
    mcp_server._sessions[1] = {
        "remote": "r", "name": "mcp-exec-reap", "profile": "cmd",
        "tmp_script": "/tmp/x.cmd", "tmp_out": "/tmp/x.out", "tmp_code": "/tmp/x.code",
    }
    for p in ("/tmp/x.cmd", "/tmp/x.out", "/tmp/x.code"):
        c.files[p] = b"x"

    assert mcp_server._reap_all_sessions(c, timeout=5.0) == 1

    assert c.killed == [{"remote": "r", "name": "mcp-exec-reap", "timeout": 5.0}]
    assert c.files == {}
    assert mcp_server._sessions == {}


# ---- self-describing output (empty / timed out) ----


class SilentFinishClient(ExecTempClient):
    """Command completes with no output at all (e.g. a redirect-only command)."""

    def send_keys(self, *, remote, name, keys, flags, timeout=30.0):
        self.sent.append(list(keys))
        m = re.match(r"(\S+) (\S+) > (\S+) 2>&1; echo \$\? > (\S+)", str(keys[0]))
        assert m, f"unexpected launch line: {keys[0]!r}"
        _, _script, out, code = m.groups()
        self.files[out] = b""
        self.files[code] = b"0"
        return {"ok": True}


class NeverFinishesClient(ExecTempClient):
    """Still printing when the wait expires: partial .out file, no .code file."""

    def __init__(self):
        super().__init__()
        self.partial = b"partial line 1\npartial line 2\n"

    def send_keys(self, *, remote, name, keys, flags, timeout=30.0):
        self.sent.append(list(keys))
        m = re.match(r"(\S+) (\S+) > (\S+) 2>&1; echo \$\? > (\S+)", str(keys[0]))
        assert m, f"unexpected launch line: {keys[0]!r}"
        _, _script, out, _code = m.groups()
        self.files[out] = self.partial  # never writes .code -> never finished
        return {"ok": True}


def test_output_markers():
    assert mcp_server._decorate_output("", finished=True, timeout_ms=10000) == \
        "<aimux>command output is empty</aimux>"
    assert mcp_server._decorate_output("   \n", finished=True, timeout_ms=10000) == \
        "<aimux>command output is empty</aimux>"
    assert mcp_server._decorate_output("hi\n", finished=True, timeout_ms=10000) == "hi\n"
    assert mcp_server._decorate_output("", finished=False, timeout_ms=10000) == \
        "<aimux>command timeout after 10 seconds</aimux>"
    assert mcp_server._decorate_output("partial\n", finished=False, timeout_ms=250) == \
        "partial\n<aimux>get some output, but command timeout after 0.25 seconds</aimux>"
    # dead session: the reader must not mistake it for "finished with no output"
    assert mcp_server._decorate_output("", finished=True, dead=True) == \
        "<aimux>session ended (shell exited)</aimux>"
    assert mcp_server._decorate_output("half\n", finished=True, dead=True) == \
        "half\n<aimux>get some output, but session ended (shell exited)</aimux>"
    assert mcp_server._seconds_label(300000) == "300"


def test_exec_with_no_output_says_it_finished_empty(monkeypatch):
    c = SilentFinishClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()

    r = mcp_server._remote_exec_command("r", "posix-pty", {"cmd": "true > /dev/null", "yield_time_ms": 1000})

    assert r["exit_code"] == 0
    assert r["output"] == "<aimux>command output is empty</aimux>"


def test_exec_timeout_keeps_partial_output_and_appends_marker(monkeypatch):
    c = NeverFinishesClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()

    r = mcp_server._remote_exec_command("r", "posix-pty", {"cmd": "sleep 99", "yield_time_ms": 1000})

    assert "session_id" in r and "exit_code" not in r
    assert r["output"].endswith("<aimux>get some output, but command timeout after 1 seconds</aimux>")
    assert "partial line 1" in r["output"]  # partial output is never dropped


def test_timeout_marker_survives_truncation(monkeypatch):
    c = NeverFinishesClient()
    c.partial = ("\n".join(f"line {i}" for i in range(2000)) + "\n").encode()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()

    r = mcp_server._remote_exec_command(
        "r", "posix-pty", {"cmd": "sleep 99", "yield_time_ms": 1000, "max_output_tokens": 50})

    assert r["output"].endswith("<aimux>get some output, but command timeout after 1 seconds</aimux>")
    assert "...[truncated]..." in r["output"]
    assert r["original_token_count"] > 50


class PartialThenDeadClient(NeverFinishesClient):
    """Printed something, then the session was killed before it finished."""

    def capture(self, **kw):
        raise bridge.BridgeError("session not found", kind="session_gone")


def test_exec_dead_after_partial_output_says_so(monkeypatch):
    monkeypatch.setattr(mcp_server, "_EXEC_PROBE_EVERY_S", 0.4)
    c = PartialThenDeadClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()

    r = mcp_server._remote_exec_command("r", "posix-pty", {"cmd": "sleep 900", "yield_time_ms": 3000})

    assert r["output"] == "partial line 1\npartial line 2\n<aimux>get some output, but session ended (shell exited)</aimux>"
    assert r["exit_code"] == -1 and r["note"] == "session ended (shell exited)"


def test_toolsets_select_tools():
    from aimux import mcp_server

    classic = mcp_server.tools_for("classic")
    combined = mcp_server.tools_for("classic-and-gui")
    assert [t["name"] for t in classic] == [t["name"] for t in mcp_server.CLASSIC_TOOLS]
    gui_names = [t["name"] for t in mcp_server.GUI_TOOLS]
    assert all(name.startswith("remote_gui_") for name in gui_names)
    assert [t["name"] for t in combined] == [t["name"] for t in classic] + gui_names
    with pytest.raises(ValueError):
        mcp_server.tools_for("gui")


def test_gui_display_help_document_returns_the_bundled_document():
    from aimux import mcp_server

    result = mcp_server._gui_display_help_document("box", "cmd", {})

    assert "remote_gui_list_windows" in result["document"]
    assert "remote_gui_display_help_document" in result["document"]
    assert result["path"].endswith("gui_help_document.py")


def test_dispatch_serves_the_gui_help_document(monkeypatch):
    from aimux import mcp_server

    results: list[dict] = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: results.append(obj))
    monkeypatch.setattr(mcp_server, "_ACTIVE_TOOLSET", "classic-and-gui")

    mcp_server._dispatch("box", "cmd", {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "remote_gui_display_help_document", "arguments": {}},
    })

    payload = json.loads(results[-1]["result"]["content"][0]["text"])
    assert "remote_gui_list_windows" in payload["document"]


def test_dispatch_hides_gui_tools_under_classic_toolset(monkeypatch):
    from aimux import mcp_server

    results: list[dict] = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: results.append(obj))
    monkeypatch.setattr(mcp_server, "_ACTIVE_TOOLSET", "classic")

    mcp_server._dispatch("box", "cmd", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    listed = [t["name"] for t in results[-1]["result"]["tools"]]
    assert "remote_gui_look" not in listed and "remote_exec_command" in listed

    mcp_server._dispatch("box", "cmd", {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                        "params": {"name": "remote_gui_diagnostics", "arguments": {}}})
    assert "unknown tool" in str(results[-1].get("error", {}))


def _look_result(**overrides):
    payload = {
        "lookId": "look_7",
        "window": {"rootRef": "@w1"},
        "outline": {"ref": "@e1"},
        "timings": {"totalMs": 12},
        "image": {"jpegBase64": base64.b64encode(b"\x89PNG\r\n\x1a\nbytes").decode("ascii"),
                  "mimeType": "image/png"},
    }
    payload.update(overrides)
    return payload


def test_gui_look_spools_screenshot_and_returns_a_path(monkeypatch, tmp_path):
    from aimux import mcp_server

    monkeypatch.setattr(mcp_server, "_gui_shot_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_server, "_gui_call", lambda remote, cmd, args, **kw: _look_result())

    out = mcp_server._gui_look("box", "cmd", {"root_ref": "@w1"})

    # A base64 payload in the result is what overran the host token limit.
    assert "image_url" not in out
    assert out["image_path"].startswith(str(tmp_path))
    with open(out["image_path"], "rb") as fh:
        assert fh.read() == b"\x89PNG\r\n\x1a\nbytes"
    assert out["lookId"] == "look_7" and out["outline"] == {"ref": "@e1"}


def test_gui_look_spools_screenshot_even_when_the_outline_is_truncated(monkeypatch, tmp_path):
    from aimux import mcp_server

    monkeypatch.setattr(mcp_server, "_gui_shot_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_server, "_gui_call",
                        lambda remote, cmd, args, **kw: _look_result(outline={"x": "y" * 5000}))

    out = mcp_server._gui_look("box", "cmd", {"root_ref": "@w1", "max_output_tokens": 10})

    assert out["outline_truncated"] is True and "outline" not in out
    assert out["image_path"].endswith(".png") and "image_url" not in out


def test_gui_look_omits_image_path_when_the_helper_sends_no_image(monkeypatch, tmp_path):
    from aimux import mcp_server

    monkeypatch.setattr(mcp_server, "_gui_shot_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_server, "_gui_call",
                        lambda remote, cmd, args, **kw: _look_result(image=None))

    out = mcp_server._gui_look("box", "cmd", {"root_ref": "@w1"})

    assert "image_path" not in out and "image_url" not in out
    assert list(tmp_path.glob("*.png")) == []


def test_prune_gui_shots_keeps_the_newest_and_drops_expired(monkeypatch, tmp_path):
    from aimux import mcp_server

    monkeypatch.setattr(mcp_server, "_GUI_SHOT_MAX_FILES", 2)
    now = time.time()
    # Recent enough to survive the TTL, ordered newest-last.
    for name, age in (("a.png", 30), ("b.png", 20), ("c.png", 10), ("stale.png", 10 * 60 * 60)):
        path = tmp_path / name
        path.write_bytes(b"x")
        os.utime(path, (now - age, now - age))

    mcp_server._prune_gui_shots(tmp_path)

    assert sorted(p.name for p in tmp_path.glob("*.png")) == ["b.png", "c.png"]


# ---- remote_gui_record_window_mp4 ----


class _RecordingClient:
    """Bridge client stub: file_stat + the two read paths the fetch may take."""

    def __init__(self, payload=b"\x00\x00\x00\x18ftypmp42", size=None):
        self.payload = payload
        self.size = len(payload) if size is None else size
        self.streamed = False
        self.read_paths = []

    def file_stat(self, *, remote, path, timeout=60.0):
        return {"size": self.size, "path": path}

    def file_read(self, *, remote, path, timeout=180.0):
        self.read_paths.append(path)
        return {"content_b64": base64.b64encode(self.payload).decode("ascii")}

    def file_read_stream(self, *, remote, path, timeout=180.0, progress=None, total=0):
        self.streamed = True
        self.read_paths.append(path)
        return self.payload


def _record_calls(monkeypatch, *, status_active=False, stop=None, start=None):
    """Drive the handler against a fake helper; returns (client, calls)."""
    calls: list[tuple[str, dict]] = []

    def fake_call(remote, cmd, args, **kw):
        calls.append((cmd, args))
        if cmd == "recordStart":
            return start or {"recordingId": "rec_1", "path": "C:\\Temp\\a.mp4", "width": 640,
                             "height": 480, "fps": 10, "audio": "system", "hasAudio": True}
        if cmd == "recordStatus":
            return {"active": status_active}
        if cmd == "recordStop":
            return stop or {"recordingId": "rec_1", "path": "C:\\Temp\\a.mp4", "bytes": 20,
                            "durationMs": 1000, "width": 640, "height": 480, "fps": 10,
                            "audio": "system", "hasAudio": True, "videoFrames": 10,
                            "audioFrames": 46, "stopped": True}
        raise AssertionError(f"unexpected helper command {cmd}")

    client = _RecordingClient()
    monkeypatch.setattr(mcp_server, "_gui_call", fake_call)
    monkeypatch.setattr(mcp_server, "_client", lambda: client)
    monkeypatch.setattr(mcp_server, "_remote_platform", "windows")
    monkeypatch.setattr(mcp_server, "_GUI_RECORD_POLL_SECONDS", 0.01)
    return client, calls


def test_gui_record_window_mp4_downloads_the_mp4_and_returns_a_local_path(monkeypatch, tmp_path):
    client, calls = _record_calls(monkeypatch, status_active=True)
    monkeypatch.setattr(mcp_server, "_gui_video_dir", lambda: tmp_path)

    out = mcp_server._gui_record_window_mp4("box", "cmd", {"root_ref": "@w1", "duration_ms": 1000})

    names = [cmd for cmd, _ in calls]
    # recordStatus is polled until the deadline, so only the ends are fixed.
    assert names[0] == "recordStart" and names[-1] == "recordStop"
    assert set(names[1:-1]) <= {"recordStatus"}
    assert calls[0][1] == {"rootRef": "@w1", "fps": 10, "maxDurationMs": 1000,
                           "maxDimension": 0, "audio": "system"}
    assert out["video_path"].startswith(str(tmp_path)) and out["video_path"].endswith(".mp4")
    with open(out["video_path"], "rb") as fh:
        assert fh.read() == client.payload
    assert out["remote_path"] == "C:\\Temp\\a.mp4" and out["recording_id"] == "rec_1"
    assert out["has_audio"] is True and out["video_frames"] == 10 and out["size"] == len(client.payload)
    assert out["max_dimension"] is None  # no cap: encoded at the window's own size
    assert "audio_warning" not in out and client.streamed is False


def test_gui_record_window_mp4_passes_record_fps_and_the_size_cap(monkeypatch, tmp_path):
    _, calls = _record_calls(
        monkeypatch, status_active=True,
        start={"recordingId": "rec_1", "path": "C:\\Temp\\a.mp4", "width": 1280, "height": 802,
               "fps": 15, "maxDimension": 1280, "audio": "none", "hasAudio": False},
        stop={"recordingId": "rec_1", "path": "C:\\Temp\\a.mp4", "bytes": 20, "durationMs": 1000,
              "width": 1280, "height": 802, "fps": 15, "audio": "none", "hasAudio": False,
              "videoFrames": 15, "stopped": True})
    monkeypatch.setattr(mcp_server, "_gui_video_dir", lambda: tmp_path)

    out = mcp_server._gui_record_window_mp4(
        "box", "cmd", {"root_ref": "@w1", "duration_ms": 1000, "record_fps": 15,
                       "max_dimension": 1280, "audio": "none"})

    assert calls[0][1]["fps"] == 15 and calls[0][1]["maxDimension"] == 1280
    assert (out["width"], out["height"]) == (1280, 802) and out["fps"] == 15
    # The helper's own answer, so a cap it clamped is what the caller sees.
    assert out["max_dimension"] == 1280


def test_gui_record_window_mp4_still_accepts_the_older_fps_name(monkeypatch, tmp_path):
    _, calls = _record_calls(monkeypatch, status_active=True)
    monkeypatch.setattr(mcp_server, "_gui_video_dir", lambda: tmp_path)

    mcp_server._gui_record_window_mp4("box", "cmd", {"root_ref": "@w1", "duration_ms": 1000, "fps": 20})

    assert calls[0][1]["fps"] == 20
    assert calls[0][1]["maxDimension"] == 0


def test_gui_record_window_mp4_streams_a_large_recording(monkeypatch, tmp_path):
    client, _ = _record_calls(monkeypatch, status_active=True)
    client.size = mcp_server._GUI_VIDEO_STREAM_THRESHOLD + 1
    monkeypatch.setattr(mcp_server, "_gui_video_dir", lambda: tmp_path)

    mcp_server._gui_record_window_mp4("box", "cmd", {"root_ref": "@w1", "duration_ms": 1000})

    assert client.streamed is True  # base64-in-one-message does not scale to MP4s


def test_gui_record_window_mp4_stops_early_when_the_window_disappears(monkeypatch, tmp_path):
    _record_calls(monkeypatch, status_active=False)
    monkeypatch.setattr(mcp_server, "_gui_video_dir", lambda: tmp_path)

    out = mcp_server._gui_record_window_mp4("box", "cmd", {"root_ref": "@w1", "duration_ms": 600000})

    # A closed window (or the helper's own cap) must not hold the tool for 10 min.
    assert out["duration_ms"] == 1000


def test_gui_record_window_mp4_reports_audio_warnings_and_survives_them(monkeypatch, tmp_path):
    _, calls = _record_calls(
        monkeypatch, status_active=True,
        stop={"recordingId": "rec_1", "path": "C:\\Temp\\a.mp4", "bytes": 20, "durationMs": 1000,
              "audio": "system", "hasAudio": False, "audioWarning": "no default render device"})
    monkeypatch.setattr(mcp_server, "_gui_video_dir", lambda: tmp_path)

    out = mcp_server._gui_record_window_mp4("box", "cmd", {"root_ref": "@w1", "duration_ms": 1000})

    assert out["audio_warning"] == "no default render device" and out["video_path"]


def test_gui_record_window_mp4_raises_when_the_recording_produced_no_file(monkeypatch, tmp_path):
    _record_calls(monkeypatch, status_active=True,
                  stop={"recordingId": "rec_1", "path": "C:\\Temp\\a.mp4", "bytes": 0,
                        "error": "capture failed: the window is gone"})
    monkeypatch.setattr(mcp_server, "_gui_video_dir", lambda: tmp_path)

    with pytest.raises(RuntimeError, match="the window is gone"):
        mcp_server._gui_record_window_mp4("box", "cmd", {"root_ref": "@w1", "duration_ms": 1000})


def test_gui_record_window_mp4_fails_immediately_off_windows(monkeypatch):
    from aimux import mcp_server as m

    def boom(*a, **kw):
        raise AssertionError("a non-Windows remote must not be called")

    monkeypatch.setattr(m, "_gui_call", boom)
    monkeypatch.setattr(m, "_remote_platform", "linux")

    with pytest.raises(RuntimeError, match="only supported on Windows"):
        m._gui_record_window_mp4("box", "posix-pty", {"root_ref": "@w1"})


def test_gui_record_window_mp4_validates_its_arguments(monkeypatch, tmp_path):
    _record_calls(monkeypatch, status_active=True)
    monkeypatch.setattr(mcp_server, "_gui_video_dir", lambda: tmp_path)

    with pytest.raises(ValueError, match="root_ref is required"):
        mcp_server._gui_record_window_mp4("box", "cmd", {})
    with pytest.raises(ValueError, match="audio must be one of"):
        mcp_server._gui_record_window_mp4("box", "cmd", {"root_ref": "@w1", "audio": "loopback"})
    with pytest.raises(ValueError, match="record_fps must be a number"):
        mcp_server._gui_record_window_mp4("box", "cmd", {"root_ref": "@w1", "record_fps": "fast"})
    with pytest.raises(ValueError, match="max_dimension must be a number"):
        mcp_server._gui_record_window_mp4("box", "cmd", {"root_ref": "@w1", "max_dimension": "big"})
    with pytest.raises(ValueError, match="max_dimension cannot be negative"):
        mcp_server._gui_record_window_mp4("box", "cmd", {"root_ref": "@w1", "max_dimension": -5})


def test_gui_record_window_mp4_honours_an_explicit_local_path(monkeypatch, tmp_path):
    _record_calls(monkeypatch, status_active=True)
    target = tmp_path / "clips" / "demo.mp4"

    out = mcp_server._gui_record_window_mp4(
        "box", "cmd", {"root_ref": "@w1", "duration_ms": 1000, "local_path": str(target)})
    assert out["video_path"] == str(target) and target.is_file()

    with pytest.raises(FileExistsError):
        mcp_server._gui_record_window_mp4(
            "box", "cmd", {"root_ref": "@w1", "duration_ms": 1000, "local_path": str(target)})
    out = mcp_server._gui_record_window_mp4(
        "box", "cmd", {"root_ref": "@w1", "duration_ms": 1000, "local_path": str(target), "overwrite": True})
    assert out["video_path"] == str(target)


def test_prune_gui_videos_uses_the_same_policy_as_shots(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp_server, "_GUI_VIDEO_MAX_FILES", 1)
    now = time.time()
    for name, age in (("old.mp4", 40), ("new.mp4", 10), ("stale.mp4", 10 * 60 * 60)):
        path = tmp_path / name
        path.write_bytes(b"x")
        os.utime(path, (now - age, now - age))

    mcp_server._prune_gui_spool(tmp_path, "*.mp4", mcp_server._GUI_VIDEO_TTL_SECONDS,
                                mcp_server._GUI_VIDEO_MAX_FILES)

    assert [p.name for p in tmp_path.glob("*.mp4")] == ["new.mp4"]


# ---- default workdir (remote_set_remote_workdir) ----


@pytest.fixture(autouse=True)
def _reset_workdir_state(monkeypatch, tmp_path):
    """Keep the default-workdir state from leaking between tests."""
    monkeypatch.setattr(mcp_server, "_mobius_session", "")
    monkeypatch.setattr(mcp_server, "_workdir_default", "")
    monkeypatch.setattr(mcp_server, "_WORKDIR_CACHE", None)
    monkeypatch.setattr(mcp_server, "_WORKDIR_STORE", tmp_path / "mcp_workdirs.json")


class WorkdirClient(ExecTempClient):
    """Records the cwd each exec session was created with, and knows some dirs."""

    def __init__(self, *, dirs=("/srv/app",)):
        super().__init__()
        self.dirs = set(dirs)
        self.created: list[str] = []
        self.session_cwds: list[str | None] = []

    def create_session(self, **kw):
        self.session_cwds.append(kw.get("cwd"))
        return {"ok": True}

    def file_stat(self, *, remote, path, timeout=60.0):
        if path in self.dirs:
            return {"exists": True, "type": "dir", "size": 0}
        if path in self.files:
            return super().file_stat(remote=remote, path=path, timeout=timeout)
        return {"exists": False, "type": "none", "size": 0}

    def file_mkdir(self, *, remote, path, timeout=60.0):
        self.created.append(path)
        self.dirs.add(path)
        return {"ok": True}


def _client_for(monkeypatch, c):
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()


def test_workdir_default_is_used_when_the_argument_is_omitted(monkeypatch):
    c = WorkdirClient()
    _client_for(monkeypatch, c)

    assert mcp_server._remote_set_remote_workdir("r", "posix-pty", {"workdir": "/srv/app"}) == {
        "ok": True, "scope": "process", "remote": "r", "workdir": "/srv/app", "verified": "dir",
    }

    res = mcp_server._remote_exec_command("r", "posix-pty", {"cmd": "pwd", "yield_time_ms": 1000})

    assert c.session_cwds == ["/srv/app"]          # the session was created in the default
    assert res["workdir"] == "/srv/app"
    assert res["workdir_source"] == "session_default"


def test_explicit_workdir_beats_the_default(monkeypatch):
    c = WorkdirClient(dirs=("/srv/app", "/tmp"))
    _client_for(monkeypatch, c)
    mcp_server._remote_set_remote_workdir("r", "posix-pty", {"workdir": "/srv/app"})

    res = mcp_server._remote_exec_command(
        "r", "posix-pty", {"cmd": "pwd", "workdir": "/tmp", "yield_time_ms": 1000})

    assert c.session_cwds == ["/tmp"]
    assert res["workdir"] == "/tmp" and res["workdir_source"] == "explicit"
    # the default is not consumed: the next call without workdir still uses it
    mcp_server._remote_exec_command("r", "posix-pty", {"cmd": "pwd", "yield_time_ms": 1000})
    assert c.session_cwds == ["/tmp", "/srv/app"]


def test_clearing_the_default_returns_to_the_client_directory(monkeypatch):
    c = WorkdirClient()
    _client_for(monkeypatch, c)
    mcp_server._remote_set_remote_workdir("r", "posix-pty", {"workdir": "/srv/app"})

    cleared = mcp_server._remote_set_remote_workdir("r", "posix-pty", {"workdir": ""})
    assert cleared["workdir"] is None and cleared["verified"] == "cleared"

    res = mcp_server._remote_exec_command("r", "posix-pty", {"cmd": "pwd", "yield_time_ms": 1000})
    assert c.session_cwds == [None]
    assert res["workdir"] is None and res["workdir_source"] == "client_cwd"


def test_set_workdir_validates_against_the_remote(monkeypatch):
    c = WorkdirClient()
    _client_for(monkeypatch, c)

    with pytest.raises(ValueError, match="absolute path"):
        mcp_server._remote_set_remote_workdir("r", "posix-pty", {"workdir": "relative/dir"})
    with pytest.raises(ValueError, match="does not exist"):
        mcp_server._remote_set_remote_workdir("r", "posix-pty", {"workdir": "/nope"})

    made = mcp_server._remote_set_remote_workdir(
        "r", "posix-pty", {"workdir": "/srv/new", "create": True})
    assert made["verified"] == "created" and c.created == ["/srv/new"]

    # a file where a directory was expected
    c.files["/etc/hosts"] = b"x"
    with pytest.raises(ValueError, match="not a directory"):
        mcp_server._remote_set_remote_workdir("r", "posix-pty", {"workdir": "/etc/hosts"})


def test_workdir_scope_follows_the_mobius_session(monkeypatch):
    c = WorkdirClient()
    _client_for(monkeypatch, c)
    monkeypatch.setattr(mcp_server, "_mobius_session", "sess-1")

    saved = mcp_server._remote_set_remote_workdir("r", "posix-pty", {"workdir": "/srv/app"})
    assert saved["scope"] == "session"

    # A fresh MCP process in the same session still sees it (persisted), but
    # another session does not: the key is (mobius session, bridge client).
    monkeypatch.setattr(mcp_server, "_workdir_default", "")
    monkeypatch.setattr(mcp_server, "_WORKDIR_CACHE", None)
    assert mcp_server._effective_workdir({}, "r") == ("/srv/app", "session_default")

    monkeypatch.setattr(mcp_server, "_mobius_session", "sess-2")
    monkeypatch.setattr(mcp_server, "_WORKDIR_CACHE", None)
    assert mcp_server._effective_workdir({}, "r") == (None, "client_cwd")
    # ... and the same session pointed at another bridge client, likewise
    monkeypatch.setattr(mcp_server, "_mobius_session", "sess-1")
    assert mcp_server._effective_workdir({}, "other_client") == (None, "client_cwd")


def test_workdir_store_drops_stale_and_corrupt_entries(monkeypatch, tmp_path):
    store = tmp_path / "mcp_workdirs.json"
    monkeypatch.setattr(mcp_server, "_WORKDIR_STORE", store)
    monkeypatch.setattr(mcp_server, "_WORKDIR_CACHE", None)
    store.write_text(
        '{"sess-1|r": {"workdir": "/srv/app", "updated_at": %d},'
        ' "sess-1|old": {"workdir": "/gone", "updated_at": 1},'
        ' "junk": "not-a-dict"}' % time.time(),
        encoding="utf-8")
    assert list(mcp_server._load_workdir_store()) == ["sess-1|r"]

    store.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(mcp_server, "_WORKDIR_CACHE", None)
    assert mcp_server._load_workdir_store() == {}   # a corrupt store must not raise


# ---- GUI operation guide shipped with the package ----

def test_gui_diagnostics_reports_the_bundled_skill_path(monkeypatch):
    monkeypatch.setattr(mcp_server, "_gui_call", lambda remote, cmd, args, **kw: {"ok": True, "protocol": 1})

    res = mcp_server._gui_diagnostics("r", "p", {})

    assert res["ok"] is True                      # the helper's own fields survive
    path = res["skill_path"]
    assert path.endswith("skills/mobius-gui-operation/SKILL.md")
    assert Path(path).is_file(), f"the guide is not packaged: {path}"
    assert "GUI操作方法" in Path(path).read_text(encoding="utf-8")


def test_gui_skill_path_is_none_when_packaging_is_broken(monkeypatch):
    import importlib.resources

    def boom(_name):
        raise ModuleNotFoundError("aimux")

    monkeypatch.setattr(importlib.resources, "files", boom)
    assert mcp_server.gui_skill_path() is None


def test_every_gui_tool_description_points_at_diagnostics_first():
    for tool in mcp_server.GUI_TOOLS:
        assert "remote_gui_diagnostics" in tool["description"], tool["name"]
        assert "skill_path" in tool["description"], tool["name"]
