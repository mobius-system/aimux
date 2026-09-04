"""remote_write_stdin — real interactive sessions: python REPL, less, vim,
nested tmux, heredoc, getpass. 6 tests.

Control-flow note: the write that completes the underlying command (marker
flushes) returns the *finished* result and reaps the session — so assertions
for "program exited / printed X" read that write's own return value; polling
afterwards would hit "unknown or expired session_id".
"""
from __future__ import annotations

import time

import pytest

from conftest import wait_for_output


def _exec(mcp, cmd, **kw):
    return mcp.call("remote_exec_command", cmd=cmd, yield_time_ms=kw.pop("yield_ms", 800), **kw)


def test_write_stdin_python_repl_eval(mcp):
    res = _exec(mcp, "python3 -i")
    sid = res["session_id"]
    try:
        mcp.call("remote_write_stdin", session_id=sid, chars="print(1+1)", yield_time_ms=1200)
        out = wait_for_output(mcp, sid, "2", timeout=8.0)
        assert "2" in out, out
    finally:
        mcp.call("remote_write_stdin", session_id=sid, chars="quit()\n", append_enter=False, yield_time_ms=500)


def test_write_stdin_less_quit_ends_session(mcp):
    res = _exec(mcp, "less /etc/hostname", yield_ms=2500)
    sid = res["session_id"]
    # `q` is a control key → sent raw (no Enter). less exits, the pending exit
    # marker flushes, and THIS write call returns the finished result.
    out = mcp.call("remote_write_stdin", session_id=sid, chars="q", yield_time_ms=2500)
    assert out.get("exit_code") is not None, out
    assert "session_id" not in out  # reaped


def test_write_stdin_vim_insert_save(mcp, tmp_path):
    out_file = tmp_path / "vim_out.txt"
    res = _exec(mcp, "vim")
    sid = res["session_id"]
    try:
        mcp.call("remote_write_stdin", session_id=sid, chars="i", append_enter=False, yield_time_ms=600)
        mcp.call("remote_write_stdin", session_id=sid, chars="hello-from-vim", yield_time_ms=600)
        mcp.call("remote_write_stdin", session_id=sid, chars="Escape", append_enter=False, yield_time_ms=600)
        mcp.call(
            "remote_write_stdin",
            session_id=sid,
            chars=f":wq! {out_file}\n",
            append_enter=False,
            yield_time_ms=1200,
        )
        # Wait for vim to actually exit and the file to land.
        deadline = time.time() + 10
        while time.time() < deadline and not out_file.exists():
            time.sleep(0.3)
        assert out_file.exists(), "vim did not write the file"
        assert "hello-from-vim" in out_file.read_text()
    finally:
        # If vim is somehow still open, force-quit.
        try:
            mcp.call("remote_write_stdin", session_id=sid, chars="\x1b", append_enter=False, yield_time_ms=300)
            mcp.call("remote_write_stdin", session_id=sid, chars=":q!\n", append_enter=False, yield_time_ms=500)
        except Exception:
            pass


def test_write_stdin_nested_tmux_windows(mcp):
    # Interactive tmux on an isolated socket (-L) so a stray server can never
    # touch the host's real tmux sessions. Drive it with real prefix keys:
    # Ctrl-b (\x02) + c creates a 2nd window; Ctrl-b + w shows the window list.
    res = _exec(mcp, "tmux -L aimux-itest new-session -s work")
    sid = res["session_id"]
    try:
        mcp.call("remote_write_stdin", session_id=sid, chars="\x02c", append_enter=False, yield_time_ms=800)
        mcp.call("remote_write_stdin", session_id=sid, chars="\x02w", append_enter=False, yield_time_ms=1200)
        out = wait_for_output(mcp, sid, "1:", timeout=10.0)
        assert "0:" in out and "1:" in out, out
    finally:
        # kill-server via tmux command prompt (C-b :) makes the attached client
        # exit → marker flushes → session reaped. Fallback: fresh exec kill.
        try:
            mcp.call("remote_write_stdin", session_id=sid, chars="\x02:", append_enter=False, yield_time_ms=400)
            mcp.call("remote_write_stdin", session_id=sid, chars="kill-server", yield_time_ms=1500)
        except Exception:
            try:
                mcp.call("remote_exec_command", cmd="tmux -L aimux-itest kill-server", yield_time_ms=2000)
            except Exception:
                pass


def test_write_stdin_cat_heredoc(mcp):
    res = _exec(mcp, "cat << 'EOF'")
    sid = res["session_id"]
    mcp.call("remote_write_stdin", session_id=sid, chars="line-alpha", yield_time_ms=500)
    mcp.call("remote_write_stdin", session_id=sid, chars="line-beta", yield_time_ms=500)
    # Sending the terminator: the shell collects the heredoc, cat emits the
    # block, exits, marker flushes — this write returns the finished result.
    out = mcp.call("remote_write_stdin", session_id=sid, chars="EOF", yield_time_ms=2500)
    text = out.get("output", "")
    assert out.get("exit_code") is not None, out
    # PTY echo shows each typed line once; cat's own output re-emits them —
    # seeing line-beta twice proves the heredoc body really reached cat.
    assert "line-alpha" in text and "line-beta" in text, text
    assert text.count("line-beta") >= 2, text


def test_write_stdin_getpass_reads_secret(mcp):
    # getpass reads a password without echoing; the program then prints what
    # it received — proving raw-mode stdin delivery end-to-end.
    res = _exec(mcp, "python3 -c 'import getpass; print(\"got:\"+getpass.getpass(\"pwd:\"))'")
    sid = res["session_id"]
    out = wait_for_output(mcp, sid, "pwd:", timeout=8.0)
    assert "pwd:" in out, out
    # Typing the secret: getpass returns, python prints "got:sekrit" and
    # exits — this write returns the finished result (session reaped).
    res2 = mcp.call("remote_write_stdin", session_id=sid, chars="sekrit", yield_time_ms=3000)
    text = res2.get("output", "")
    assert "got:sekrit" in text, text
    assert res2.get("exit_code") == 0, res2
