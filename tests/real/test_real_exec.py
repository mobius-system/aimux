"""remote_exec_command — real PTY on a real Ubuntu kernel (6 tests).

Plus the Windows-side exit-marker string builder assertions (the only
Windows-specific surface in exec that is deterministic pure-Python of the
profile string, not of a Windows kernel) folded in here so both OSes are
covered by the suite.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from aimux import mcp_server


# ---------------------------------------------------------------------------
# 1-6: real exec via the live MCP server
# ---------------------------------------------------------------------------


def test_exec_echo_exit_code_zero(mcp):
    res = mcp.call("remote_exec_command", cmd="echo hello-aimux", yield_time_ms=2000)
    assert res.get("exit_code") == 0, res
    assert "hello-aimux" in res["output"], res["output"]
    # finished => session reaped, no dangling session_id
    assert "session_id" not in res


def test_exec_failing_command_exit_code(mcp):
    # Use a subshell so `exit` doesn't kill the marker-chaining shell.
    res = mcp.call("remote_exec_command", cmd="(exit 7)", yield_time_ms=2000)
    assert res.get("exit_code") == 7, res
    assert "session_id" not in res


def test_exec_workdir_is_honored(mcp, tmp_path):
    target = tmp_path / "deep" / "cwd"
    target.mkdir(parents=True)
    res = mcp.call("remote_exec_command", cmd="pwd", workdir=str(target), yield_time_ms=2000)
    assert res.get("exit_code") == 0, res
    assert str(target) in res["output"], res["output"]


def test_exec_long_running_returns_session_id(mcp):
    # sleep 5 but only yield 500ms → command still running, must hand back sid.
    res = mcp.call("remote_exec_command", cmd="sleep 5", yield_time_ms=500)
    assert "session_id" in res, res
    assert "exit_code" not in res
    # The session is now tracked by the MCP server; reaping happens on server
    # teardown, but we also kill it now via the per-test cleanup (broker
    # session list). Kill it explicitly so it doesn't linger:
    _kill_exec_session(mcp, res["session_id"])


def test_exec_token_truncation(mcp):
    # ~50000 chars >> 100-token budget (400 bytes). Output must be truncated.
    res = mcp.call(
        "remote_exec_command",
        cmd="python3 -c 'print(\"x\"*50000)'",
        max_output_tokens=100,
        yield_time_ms=3000,
    )
    assert res.get("exit_code") == 0, res
    assert res["original_token_count"] >= 100, res
    assert res["output"].endswith("...[truncated]..."), res["output"][-40:]


def test_exec_login_false_disables_rc(mcp, tmp_path, monkeypatch):
    # Plant a marker in ~/.bashrc that echoes to stdout when sourced. With
    # login=False (--norc --noprofile), the marker must NOT appear.
    import os
    rc = Path(os.path.expanduser("~")) / ".bashrc"
    marker = "AIMUX_RC_MARKER_9f2b"
    had = rc.exists()
    original = rc.read_text() if had else ""
    try:
        with open(rc, "a") as fh:
            fh.write(f"\necho {marker}\n")
        # login=False only takes effect with an explicit bash-like shell.
        res = mcp.call(
            "remote_exec_command",
            cmd="echo done-login-false",
            shell="/bin/bash", login=False,
            yield_time_ms=3000,
        )
        assert res.get("exit_code") == 0, res
        assert marker not in res["output"], res["output"]
        assert "done-login-false" in res["output"], res["output"]
    finally:
        if had:
            rc.write_text(original)
        else:
            try:
                rc.unlink()
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Windows exit-marker string builders (pure functions of profile) — no
# Windows kernel needed; these test the real code path for cmd/powershell.
# ---------------------------------------------------------------------------


def test_exec_marker_builder_posix():
    cmd = mcp_server._command_with_exit_marker("ls -la", "__M__", "posix-pty")
    assert cmd == "ls -la; echo __M__:$?"


def test_exec_marker_builder_windows_cmd():
    # Caret-escaped percents: '%%errorlevel%%' is batch-file syntax, and typed at
    # an interactive prompt CALL collapses it to a literal '%0%', so the marker
    # never carries the digits the drain loop matches on.
    cmd = mcp_server._command_with_exit_marker("dir", "__M__", "cmd")
    assert cmd == "dir & call echo __M__^%errorlevel^%"


def test_exec_marker_builder_windows_powershell():
    cmd = mcp_server._command_with_exit_marker("Get-Date", "__M__", "powershell")
    assert cmd == "& { Get-Date }; echo __M__$LASTEXITCODE"


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def _kill_exec_session(mcp, sid: int) -> None:
    # The MCP server tracks the session by sid; writing Ctrl-C then polling
    # forces finish so it gets reaped. Best-effort — failure here is fine,
    # the per-test broker cleanup will still reap the underlying tmux PTY.
    try:
        mcp.call("remote_write_stdin", session_id=sid, chars="\x03", yield_time_ms=1000)
    except Exception:
        pass
