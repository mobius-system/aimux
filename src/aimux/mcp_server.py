"""aimux MCP server — expose one bridge remote as MCP tools over stdio.

Codex-facing tools (6, aligned with codex native tool specs):
  remote_exec_command  Run a command (one-shot temp-script on every platform; interactive=true keeps a live PTY); return clean output + exit code, or session_id.
  remote_write_stdin   Write chars to an existing exec session, return output.
  remote_apply_patch   Apply a patch (FREEFORM input).
  remote_view_image    Return a remote image file as a data URL.
  remote_download_file Download a remote file to the local filesystem.
  remote_ping          Health check.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio).
Zero external dependencies — Python standard library only. The server binds
to a single bridge remote (``--remote <name>``); tools do not expose the
remote parameter. Logs go to stderr only (stdout is the protocol).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from . import bridge

# ---------------------------------------------------------------------------
# bridge control
# ---------------------------------------------------------------------------


def _client() -> "bridge.BridgeControlClient":
    return bridge.BridgeControlClient.from_runtime()


def _file_bytes(res: dict) -> bytes:
    """file_read returns ``{"content_b64": "..."}``."""
    v = res.get("content_b64")
    if isinstance(v, str) and v:
        return base64.b64decode(v)
    v = res.get("content")
    if isinstance(v, str) and v:
        try:
            return base64.b64decode(v)
        except Exception:
            return v.encode("utf-8")
    return b""


# ---------------------------------------------------------------------------
# unified exec session registry (session_id -> bridge session name)
# ---------------------------------------------------------------------------

_session_counter = 0
_sessions: dict[int, dict] = {}

# Bridge control client, stashed at startup so the SIGTERM handler can reach it
# without closure tricks. Set in run_stdio() after remote/profile validation.
_reap_client = None

_EXEC_SHELL = os.environ.get("AIMUX_MCP_SHELL") or os.environ.get("SHELL") or "/bin/bash"

# Result note for a session that vanished mid-wait (killed from outside, or the
# remote shell exited). exit_code is -1 because there is no code to report.
_DEAD_NOTE = "session ended (shell exited)"

# How often a wait checks that the session still exists. File polling alone
# cannot see death (the files outlive the shell), and every check costs a bridge
# round trip, so it is far slower than the 0.3s file poll.
_EXEC_PROBE_EVERY_S = 5.0

# Ceiling for a single yield_time_ms (clamped so one tool call can't pin the
# single-threaded MCP server for an unbounded time).
_MAX_YIELD_MS = 300_000  # 5 minutes


def _create_exec_session(remote: str, profile: str, cmd: str, cwd: str | None) -> int:
    global _session_counter
    name = f"mcp-exec-{uuid.uuid4().hex[:10]}"
    _client().create_session(
        remote=remote, name=name, profile=profile, cmd=cmd, cwd=cwd,
        created_at=bridge.utc_now(), created_by="aimux-mcp",
    )
    _session_counter += 1
    sid = _session_counter
    _sessions[sid] = {"remote": remote, "name": name, "profile": profile}
    return sid


def _close_exec_session(sid: int, c, *, timeout: float | None = None) -> None:
    """Kill the session and delete its temp files.

    ``timeout`` bounds each bridge call, for callers that must not block on a
    dead client (process exit); ``None`` lets each call use its own default.
    """
    sess = _sessions.pop(sid, None)
    if not sess:
        return
    kw: dict = {} if timeout is None else {"timeout": timeout}
    try:
        c.kill_session(remote=sess["remote"], name=sess["name"], **kw)
    except Exception:
        pass
    # Best-effort cleanup of the temp script/output/code files created by the
    # temp-file exec path. Missing files are a no-op on the remote.
    for key in ("tmp_script", "tmp_out", "tmp_code"):
        path = sess.get(key)
        if not path:
            continue
        try:
            c.file_delete(remote=sess["remote"], path=path, **kw)
        except Exception:
            pass


def _reap_all_sessions(c, *, timeout: float = 5.0) -> int:
    """Close every tracked exec session. Best-effort, idempotent, safe to call
    repeatedly (kill_session treats a missing session as success). Returns the
    count reaped. Called at process exit so neither the bridge PTYs this MCP
    server created nor their temp script/output/code files leak on the
    (shared) remote client.

    Iterates ONLY this process's local ``_sessions`` — never list_sessions() /
    pattern-matches — so it cannot touch a concurrent sibling MCP process's
    sessions (mcp-exec-<uuid> names are globally unique regardless). Short
    timeout bounds exit latency if the client is down.
    """
    reaped = 0
    for sid in list(_sessions.keys()):
        if sid not in _sessions:
            continue
        _close_exec_session(sid, c, timeout=timeout)
        reaped += 1
    return reaped


def _on_sigterm(signum, frame) -> None:
    """SIGTERM handler: reap tracked sessions then exit. The run_stdio stdin
    read may not wake on a signal, so reap + os._exit directly here. os._exit
    skips the run_stdio finally (which would otherwise reap a second time —
    harmless, but avoids double work and any stdout write after a partial
    response)."""
    c = _reap_client
    if c is not None:
        try:
            _reap_all_sessions(c, timeout=5.0)
        except Exception:
            pass
    os._exit(130)  # 128 + SIGTERM


def _request_parent_death_signal() -> None:
    """Exit when the parent *process* dies, using process-level semantics.

    We previously asked the kernel for a parent-death signal
    (PR_SET_PDEATHSIG), but Linux delivers that signal when the *thread* that
    forked us exits — not when the parent process exits (prctl(2): "the
    parent-death signal [may be] sent while the parent process is still
    running"). A multi-threaded client (tokio, Go, Java, ...) that spawns us
    from a recycled worker thread then kills us while the client is still
    alive (e.g. tokio's blocking pool reaps idle threads after ~10s). Poll
    os.getppid() instead: when the parent dies we are reparented (ppid becomes
    1 or a subreaper), a genuine process-level event. Best-effort; the stdin
    EOF reap remains the primary exit path, so a failed poll never leaks the
    process beyond that.
    """
    if sys.platform == "win32":
        # No reliable getppid semantics; stdin EOF still covers the common
        # case (a dead parent closes the pipe write end).
        return
    import threading

    parent = os.getppid()
    if parent == 1:
        return  # already orphaned at spawn; nothing to watch

    def _watch() -> None:
        while True:
            time.sleep(2.0)
            try:
                if os.getppid() != parent:
                    break
            except OSError:
                break
        # Parent (process) is gone: reap tracked sessions and exit, mirroring
        # the SIGTERM handler's cleanup without relying on a signal.
        _on_sigterm(signal.SIGTERM, None)

    threading.Thread(target=_watch, name="aimux-parent-watch", daemon=True).start()


# ---------------------------------------------------------------------------
# token estimate & truncation (per spec: ceil(UTF-8 bytes / 4))
# ---------------------------------------------------------------------------


def _est_tokens(text: str) -> int:
    return -(-len(text.encode("utf-8")) // 4)


def _truncate_output(text: str, max_tokens: int) -> tuple[str, int]:
    original = _est_tokens(text)
    if max_tokens <= 0 or original <= max_tokens:
        return text, original
    max_bytes = max_tokens * 4
    truncated = text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
    return truncated + "\n...[truncated]...", original


# ---------------------------------------------------------------------------
# shared drain: poll capture until marker (command finished) or yield elapsed
# ---------------------------------------------------------------------------


def _drain(c, remote: str, name: str, marker: str, yield_ms: int, expect_marker: bool):
    """Poll capture until ``marker`` appears (command finished) or yield_ms elapses.

    Returns (output, exit_code, finished, wall_seconds, dead). ``dead`` is True
    when the remote says the session itself is gone: a killed session never
    prints the marker, so without this it looks exactly like a slow command and
    the caller polls a session that no longer exists, forever.
    """
    started = time.time()
    deadline = started + max(yield_ms, 0) / 1000.0
    out = ""
    exit_code = None
    finished = False
    while True:
        if time.time() >= deadline:
            break
        time.sleep(0.3)
        try:
            cap = c.capture(remote=remote, name=name, lines=4000, ansi=False, timeout=10.0)
        except bridge.BridgeError as e:
            if e.kind in ("session_gone", "not_found"):
                return out, -1, True, time.time() - started, True
            cap = {}
        except Exception:
            cap = {}
        out = str((cap or {}).get("content") or "")
        if expect_marker and marker:
            # Match the *execution output* "MARKER:<digits>" only — NOT the
            # command echo-back, which also contains MARKER but as
            # "echo MARKER:$?" (no digits). This avoids falsely detecting a
            # finished command while a long-running one is still in flight.
            m = re.search(re.escape(marker) + r":?(\d+)", out)
            if m:
                exit_code = int(m.group(1))
                out = out[: m.start()]
                finished = True
                break
    return out, exit_code, finished, time.time() - started, False


def _seconds_label(ms: int | None) -> str:
    """``10000`` → ``10``; ``250`` → ``0.25``; ``300000`` → ``300``."""
    secs = (ms or 0) / 1000.0
    return f"{secs:g}"


def _decorate_output(output: str, *, finished: bool, timeout_ms: int | None) -> str:
    """Make a result self-describing when it has nothing to show.

    An empty ``output`` is ambiguous: the command may have printed nothing, or
    the wait may have expired before it printed anything. Both read to an agent
    as "no result", so say which one happened, and never drop what did arrive:
    a timed-out wait with partial output keeps it and says so explicitly.

    Markers are added *after* truncation so a long partial output cannot push
    the status line off the end.
    """
    if finished:
        if not output.strip():
            return "<aimux>command output is empty</aimux>"
        return output
    waited = f"after {_seconds_label(timeout_ms)} seconds"
    if not output.strip():
        return f"<aimux>command timeout {waited}</aimux>"
    return output.rstrip("\n") + "\n" + f"<aimux>get some output, but command timeout {waited}</aimux>"


def _exec_result(sid: int, c, output: str, original_tokens: int, exit_code, finished: bool, wall: float,
                 *, note: str | None = None, timeout_ms: int | None = None) -> dict:
    res = {
        "output": _decorate_output(output, finished=finished, timeout_ms=timeout_ms),
        "original_token_count": original_tokens,
        "wall_time_seconds": round(wall, 3),
    }
    if finished:
        if exit_code is not None:
            res["exit_code"] = exit_code
        if note:
            res["note"] = note
        _close_exec_session(sid, c)
    else:
        # still running: hand back session_id for remote_write_stdin
        res["session_id"] = sid
    return res


def _append_enter_for_stdin(chars: str, requested: object) -> bool:
    """Keep line-command compatibility while preserving pager control keys."""
    if isinstance(requested, bool):
        return requested
    if chars in {"q", "Q", "\x03", "\x04", "\x1b"}:
        return False
    return not chars.endswith(("\r", "\n"))


# ---------------------------------------------------------------------------
# temp-file exec: write the command verbatim to a temp script, run it with
# stdout+stderr and the exit code redirected to temp files, then read both back
# cleanly. This replaces the PTY send_keys echo + screen-scrape + exit-marker
# dance for non-interactive exec (POSIX).
# ---------------------------------------------------------------------------


def _is_windows_profile(profile: str) -> bool:
    p = (profile or "").lower()
    return "win" in p or p in {"cmd", "powershell", "pwsh"}


# Windows temp lives under the remote user's profile: every Windows shell spells
# the same directory its own way, and '~' is what the bridge client expands.
_WIN_TEMP_DIR = "AppData/Local/Temp"

# Set from the bridge registration in run_stdio(); '' when unknown (unit tests).
_remote_platform = ""


@dataclass(frozen=True)
class _ExecPlan:
    """One temp-script run: where the three files live, the script body to
    upload, and the single line to type into the session to start it."""

    script: str
    out: str
    code: str
    body: str
    launch: str


def _shell_family(profile: str) -> str:
    """Which family of shell the session runs: cmd / powershell / everything-else."""
    p = (profile or "").lower()
    if "powershell" in p or "pwsh" in p:
        return "powershell"
    if p == "cmd" or "win" in p:
        return "cmd"
    return "posix"


def _remote_is_windows(profile: str) -> bool:
    """Whether the bound remote is a Windows host (not whether the shell is)."""
    if _remote_platform:
        return _remote_platform.lower() == "windows"
    p = (profile or "").lower()
    return _is_windows_profile(profile) or "mingw" in p or "msys" in p


def _decode_text(data: bytes) -> str:
    """Decode remote file bytes, tolerating what Windows shells actually write."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    return data.decode("utf-8-sig", "replace")  # utf-8-sig drops a BOM


def _exec_temp_base() -> str:
    """Unique temp base name: incoming_command_<UTC timestamp>."""
    now = time.time()
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime(now))
    micros = int((now % 1) * 1_000_000)
    return f"incoming_command_{ts}_{micros:06d}"


def _exec_temp_paths(shell: str = "bash", *, windows: bool = False) -> tuple[str, str, str]:
    """Return (script, out, code) paths for the temp-script run.

    POSIX remotes use /tmp/<base>.{sh,out,code}. Windows remotes cannot: only the
    MSYS shells have a /tmp, and the bridge client resolves it against the
    current drive, so the MCP server and the shell would silently disagree about
    which file they mean. They use ``~/AppData/Local/Temp`` instead — the same
    directory in every Windows shell (%USERPROFILE%, $env:USERPROFILE, $HOME).
    """
    base = _exec_temp_base()
    if not windows:
        return f"/tmp/{base}.sh", f"/tmp/{base}.out", f"/tmp/{base}.code"
    ext = {"cmd": "cmd", "powershell": "ps1"}.get(shell, "sh")
    root = f"~/{_WIN_TEMP_DIR}"
    return f"{root}/{base}.{ext}", f"{root}/{base}.out", f"{root}/{base}.code"


def _win_path(path: str) -> str:
    """``~/AppData/Local/Temp/x`` → ``%USERPROFILE%\\AppData\\Local\\Temp\\x``."""
    return path.replace("/", "\\").replace("~", "%USERPROFILE%", 1)


def _exec_plan(*, cmd: str, profile: str, shell_bin: str | None, windows: bool) -> _ExecPlan:
    """Where the temp files live, what the script says, and how to start it.

    One mechanism everywhere: the command is uploaded verbatim to a temp script,
    the launcher runs it with stdout+stderr redirected to a file, and the exit
    code lands in a second file the caller polls. Which shell writes that code
    file differs:

    * POSIX / MSYS — the launcher's own trailing ``echo $?`` does it.
    * cmd — the script does it. ``%errorlevel%`` only expands at parse time, so
      it has to be evaluated *inside* the batch file, after the command line.
    * PowerShell — the script does it, and captures output itself: a PS ``>``
      writes UTF-16, which the reader (UTF-8) would turn into mojibake.
    """
    family = _shell_family(profile)
    script, outfile, codefile = _exec_temp_paths(family, windows=windows)

    if not windows:
        runner = shell_bin or "bash"
        return _ExecPlan(script, outfile, codefile, cmd,
                         f"{runner} {script} > {outfile} 2>&1; echo $? > {codefile}")

    if family == "cmd":
        body = f"@echo off\r\n{cmd}\r\necho %errorlevel% > \"{_win_path(codefile)}\"\r\n"
        return _ExecPlan(script, outfile, codefile, body,
                         f"\"{_win_path(script)}\" > \"{_win_path(outfile)}\" 2>&1")

    if family == "powershell":
        body = (
            f"$aimux_out = & {{ {cmd} }} 2>&1 | Out-String -Width 4096\r\n"
            f"[IO.File]::WriteAllText(\"{_ps_path(outfile)}\", $aimux_out)\r\n"
            f"$aimux_code = if ($null -eq $LASTEXITCODE) {{ 0 }} else {{ $LASTEXITCODE }}\r\n"
            f"[IO.File]::WriteAllText(\"{_ps_path(codefile)}\", \"$aimux_code\")\r\n"
        )
        return _ExecPlan(script, outfile, codefile, body,
                         f"powershell -NoProfile -ExecutionPolicy Bypass -File \"{_ps_path(script)}\"")

    # MSYS / Git-Bash on Windows: sh semantics, Windows-visible paths.
    launch = f"bash \"$HOME/{_WIN_TEMP_DIR}/{_basename(script)}\" > \"$HOME/{_WIN_TEMP_DIR}/{_basename(outfile)}\" 2>&1; echo $? > \"$HOME/{_WIN_TEMP_DIR}/{_basename(codefile)}\""
    return _ExecPlan(script, outfile, codefile, cmd, launch)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _ps_path(path: str) -> str:
    """PowerShell spelling of a Windows temp path (``$env:USERPROFILE\\...``)."""
    return path.replace("/", "\\").replace("~", "$env:USERPROFILE", 1)


def _read_code_file(c, remote: str, codefile: str) -> int | None:
    """Return the exit code if the code file exists, else None (not finished)."""
    try:
        st = c.file_stat(remote=remote, path=codefile, timeout=15.0)
    except Exception:
        return None
    if not st.get("exists") or st.get("type") != "file":
        return None
    try:
        res = c.file_read(remote=remote, path=codefile, timeout=15.0)
    except Exception:
        return None
    # Be liberal: BOMs, CRLF and a stray echo prefix all show up in practice, and
    # anything unparsable here means "still running" for the caller.
    match = re.search(r"-?\d+", _decode_text(_file_bytes(res)))
    return int(match.group()) if match else None


def _read_out_file(c, remote: str, outfile: str) -> str:
    """Return the redirected output, or "" if not present/readable yet."""
    try:
        st = c.file_stat(remote=remote, path=outfile, timeout=15.0)
    except Exception:
        return ""
    if not st.get("exists") or st.get("type") != "file":
        return ""
    try:
        res = c.file_read(remote=remote, path=outfile, timeout=60.0)
    except Exception:
        return ""
    return _decode_text(_file_bytes(res))


def _session_is_gone(c, remote: str, name: str) -> bool:
    """True when the remote no longer has this session.

    The temp-script poll reads *files*, and files outlive the shell that wrote
    them: a session killed from outside leaves the poll watching a code file
    that will never appear — indistinguishable from a slow command. ``capture``
    is the liveness probe the bridge already exposes: it raises ``session_gone``
    for a session that is not there.
    """
    try:
        c.capture(remote=remote, name=name, lines=1, ansi=False, timeout=10.0)
        return False
    except bridge.BridgeError as e:
        return e.kind in ("session_gone", "not_found")
    except Exception:
        return False  # a transient error is not proof of death


def _wait_exec_done(c, remote: str, outfile: str, codefile: str, yield_ms: int,
                    *, name: str | None = None, probe_every: float | None = None):
    """Poll for the exit-code file; return (output, exit_code, finished, wall, dead).

    ``name`` enables a periodic liveness probe every ``probe_every`` seconds
    (default ``_EXEC_PROBE_EVERY_S``), so a session that died mid-wait is
    reported instead of polled forever.
    """
    probe_every = _EXEC_PROBE_EVERY_S if probe_every is None else probe_every
    started = time.time()
    deadline = started + max(yield_ms, 0) / 1000.0
    last_probe = started
    while True:
        if time.time() >= deadline:
            return _read_out_file(c, remote, outfile), None, False, time.time() - started, False
        time.sleep(0.3)
        code = _read_code_file(c, remote, codefile)
        if code is not None:
            return _read_out_file(c, remote, outfile), code, True, time.time() - started, False
        if name and time.time() - last_probe >= probe_every:
            last_probe = time.time()
            if _session_is_gone(c, remote, name):
                return _read_out_file(c, remote, outfile), -1, True, time.time() - started, True


# ---------------------------------------------------------------------------
# remote_exec_command
# ---------------------------------------------------------------------------


def _exit_marker_cmd(marker: str, profile: str) -> str:
    """Build the exit-code echo command for the session's shell family.
    bash/mingw/posix/mac → echo MARKER:$?
    Windows cmd          → echo MARKER%errorlevel%
    PowerShell           → echo MARKER$LASTEXITCODE
    """
    sh = (profile or "").lower()
    if "powershell" in sh or "pwsh" in sh:
        return f"echo {marker}$LASTEXITCODE"
    if sh == "cmd" or "win" in sh:
        return f"echo {marker}%errorlevel%"
    return f"echo {marker}:$?"


def _command_with_exit_marker(cmd: str, marker: str, profile: str) -> str:
    """Keep the exit marker in the shell parser, never in child stdin."""
    marker_cmd = _exit_marker_cmd(marker, profile)
    sh = (profile or "").lower()
    if "powershell" in sh or "pwsh" in sh:
        return f"& {{ {cmd} }}; {marker_cmd}"
    if sh == "cmd" or "win" in sh:
        # CALL triggers a second percent-expansion pass after ``cmd`` exits, so
        # ``%errorlevel%`` is read *after* the command rather than before it. The
        # braces must be caret-escaped: ``%%errorlevel%%`` is the batch-file
        # spelling and, typed at an interactive prompt, CALL turns it into a
        # literal ``%0%`` — a marker that never matches the digits the reader
        # looks for. See ``_exec_plan`` for the one-shot path.
        return f"{cmd} & call echo {marker}^%errorlevel^%"
    return f"{cmd}; {marker_cmd}"


def _remote_exec_command_pty(remote: str, profile: str, args: dict) -> dict:
    cmd = args.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("cmd is required")
    workdir = args.get("workdir") or None
    shell_bin = args.get("shell") or None
    login = args.get("login")
    if login is None:
        login = True
    # shell=None → client adapter default (Windows cmd.exe / Linux-Mac login shell).
    # login=False (--norc --noprofile) only applies to an explicit bash-like shell.
    if shell_bin:
        shell_cmd = shell_bin if login else f"{shell_bin} --norc --noprofile"
    else:
        shell_cmd = None
    max_tokens = int(args.get("max_output_tokens") or 10000)
    yield_ms = max(250, min(_MAX_YIELD_MS, int(args.get("yield_time_ms") or 10000)))

    c = _client()
    max_retries = 2  # 3 total attempts
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        sid = _create_exec_session(remote, profile, shell_cmd, workdir)
        name = _sessions[sid]["name"]
        marker = f"__AIMUX_EXIT_{uuid.uuid4().hex[:8]}__"
        _sessions[sid]["marker"] = marker
        try:
            c.send_keys(
                remote=remote,
                name=name,
                keys=[_command_with_exit_marker(cmd, marker, profile), "Enter"],
                flags=[],
                timeout=15.0,
            )
            out, exit_code, finished, wall, dead = _drain(c, remote, name, marker, yield_ms, True)
            out_t, original = _truncate_output(out, max_tokens)
            return _exec_result(sid, c, out_t, original, exit_code, finished, wall,
                                note=_DEAD_NOTE if dead else None, timeout_ms=yield_ms)
        except bridge.BridgeError as e:
            _close_exec_session(sid, c)
            if e.kind in ("session_gone", "not_found") and attempt < max_retries:
                last_error = e
                print(f"aimux-mcp: exec session lost ({e.kind}), retrying ({attempt + 1}/{max_retries})...",
                      file=sys.stderr)
                continue
            raise
        except Exception:
            _close_exec_session(sid, c)
            raise

    # All retries exhausted — should not reach here because the last attempt
    # either returns or re-raises, but keep as a safety net.
    assert last_error is not None
    raise last_error


def _remote_exec_command_tempfile(remote: str, profile: str, args: dict) -> dict:
    cmd = args.get("cmd")
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("cmd is required")
    workdir = args.get("workdir") or None
    shell_bin = args.get("shell") or None
    max_tokens = int(args.get("max_output_tokens") or 10000)
    yield_ms = max(250, min(_MAX_YIELD_MS, int(args.get("yield_time_ms") or 10000)))

    c = _client()
    windows = _remote_is_windows(profile)
    max_retries = 2  # 3 total attempts
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        sid = _create_exec_session(remote, profile, shell_bin, workdir)
        name = _sessions[sid]["name"]
        plan = _exec_plan(cmd=cmd, profile=profile, shell_bin=shell_bin, windows=windows)
        _sessions[sid].update(tmp_script=plan.script, tmp_out=plan.out, tmp_code=plan.code)
        try:
            # 1) command verbatim -> temp script (no send_keys mangling).
            c.file_write(
                remote=remote,
                path=plan.script,
                content_b64=base64.b64encode(plan.body.encode("utf-8")).decode("ascii"),
                timeout=60.0,
            )
            # 2) trivial launch line -> redirect output + exit code to files.
            c.send_keys(remote=remote, name=name, keys=[plan.launch, "Enter"], flags=[], timeout=15.0)
            # 3) poll the exit-code file, then read clean output + exit code.
            out, exit_code, finished, wall, dead = _wait_exec_done(
                c, remote, plan.out, plan.code, yield_ms, name=name)
            out_t, original = _truncate_output(out, max_tokens)
            return _exec_result(sid, c, out_t, original, exit_code, finished, wall,
                                note=_DEAD_NOTE if dead else None, timeout_ms=yield_ms)
        except bridge.BridgeError as e:
            _close_exec_session(sid, c)
            if e.kind in ("session_gone", "not_found") and attempt < max_retries:
                last_error = e
                print(f"aimux-mcp: exec session lost ({e.kind}), retrying ({attempt + 1}/{max_retries})...",
                      file=sys.stderr)
                continue
            raise
        except Exception:
            _close_exec_session(sid, c)
            raise

    assert last_error is not None
    raise last_error


def _remote_exec_command(remote: str, profile: str, args: dict) -> dict:
    # Default is the clean one-shot temp-script path on every platform.
    # Interactive commands (REPL, pager, password prompt, heredoc — anything
    # driven via remote_write_stdin) must opt into the live-PTY path with
    # interactive=true. Windows used to be forced down that path because cmd
    # could not report its exit code; the temp script now writes it to a file
    # there too, so a Windows remote gets the same clean output as any other.
    if args.get("interactive"):
        return _remote_exec_command_pty(remote, profile, args)
    return _remote_exec_command_tempfile(remote, profile, args)


# ---------------------------------------------------------------------------
# remote_write_stdin
# ---------------------------------------------------------------------------


def _remote_write_stdin(remote: str, profile: str, args: dict) -> dict:
    try:
        sid = int(args.get("session_id"))
    except Exception:
        raise ValueError("session_id is required")
    sess = _sessions.get(sid)
    if not sess:
        raise ValueError(f"unknown or expired session_id: {sid}")
    chars = args.get("chars") or ""
    has_chars = bool(chars)
    append_enter = _append_enter_for_stdin(chars, args.get("append_enter"))
    max_tokens = int(args.get("max_output_tokens") or 10000)
    default_yield = 250 if has_chars else 5000
    yield_ms = max(0, min(_MAX_YIELD_MS, int(args.get("yield_time_ms") or default_yield)))

    name = sess["name"]
    c = _client()
    try:
        if has_chars:
            keys = [chars, "Enter"] if append_enter else [chars]
            c.send_keys(remote=remote, name=name, keys=keys, flags=[], timeout=15.0)
        if sess.get("tmp_code"):
            # temp-file session: completion is signalled by the exit-code file;
            # read clean output + exit code from the redirected temp files.
            out, exit_code, finished, wall, dead = _wait_exec_done(
                c, remote, sess["tmp_out"], sess["tmp_code"], yield_ms, name=name
            )
        else:
            # legacy PTY marker session (Windows exec): poll capture for the
            # outstanding marker set by exec. An interactive child (pager,
            # password prompt, raw stdin reader) must never consume the marker
            # as stdin, so keep the marker out of the write path.
            marker = sess.get("marker", "")
            out, exit_code, finished, wall, dead = _drain(c, remote, name, marker, yield_ms, bool(marker))
    except bridge.BridgeError as e:
        if e.kind in ("session_gone", "not_found") and not has_chars:
            # Empty poll on a dead session: the remote shell exited (or was
            # killed). Report as finished so callers see a clean terminal state
            # instead of a cryptic error.
            _close_exec_session(sid, c)
            return {
                "output": _decorate_output("", finished=True, timeout_ms=None),
                "original_token_count": 0,
                "wall_time_seconds": 0,
                "exit_code": -1,
                "note": _DEAD_NOTE,
            }
        # Session gone mid-write or any other error: clean up and re-raise.
        _close_exec_session(sid, c)
        raise
    out_t, original = _truncate_output(out, max_tokens)
    return _exec_result(sid, c, out_t, original, exit_code, finished, wall,
                        note=_DEAD_NOTE if dead else None, timeout_ms=yield_ms)


# ---------------------------------------------------------------------------
# remote_view_image
# ---------------------------------------------------------------------------


def _remote_view_image(remote: str, profile: str, args: dict) -> dict:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    detail = args.get("detail") or "high"
    if detail not in ("high", "original"):
        detail = "high"
    res = _client().file_read(remote=remote, path=path, timeout=180.0)
    raw = _file_bytes(res)
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "gif": "gif",
            "webp": "webp", "bmp": "bmp"}.get(ext, ext or "octet-stream")
    b64 = base64.b64encode(raw).decode("ascii")
    return {"detail": detail, "image_url": f"data:image/{mime};base64,{b64}"}


# ---------------------------------------------------------------------------
# remote_download_file
# ---------------------------------------------------------------------------


def _remote_download_file(remote: str, profile: str, args: dict) -> dict:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    local_raw = args.get("local_path")
    if local_raw is not None and (not isinstance(local_raw, str) or not local_raw.strip()):
        raise ValueError("local_path must be a non-empty string when provided")
    overwrite = bool(args.get("overwrite", False))

    local = Path(local_raw.strip()) if local_raw else Path(os.path.basename(path))
    if not local.name or local.is_dir():
        raise ValueError(f"local_path resolves to a directory: {local}")
    local = local.expanduser().resolve()
    if local.exists() and not overwrite:
        raise FileExistsError(f"local file already exists (pass overwrite=true to replace): {local}")

    res = _client().file_read(remote=remote, path=path, timeout=180.0)
    raw = _file_bytes(res)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(raw)
    return {
        "local_path": str(local),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


# ---------------------------------------------------------------------------
# remote_ping
# ---------------------------------------------------------------------------


def _remote_ping(remote: str, profile: str, args: dict) -> dict:
    return _client().health()


# ---------------------------------------------------------------------------
# remote_apply_patch (FREEFORM input: raw codex patch text)
# ---------------------------------------------------------------------------


def _remote_apply_patch(remote: str, profile: str, args: dict) -> dict:
    if isinstance(args, dict):
        patch_text = args.get("input")
        if patch_text is None:
            patch_text = args.get("patch") or args.get("text") or ""
    else:
        patch_text = str(args or "")
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise ValueError("input (raw patch text) is required")
    applied = _apply_codex_patch(remote, patch_text)
    return {"applied": applied}


def _resolve_patch_path(path: str, workdir: str | None = None) -> str:
    """Patch paths must be absolute.

    Bridge file operations run in the reverse client's process cwd, which is
    unrelated to the agent's remote session cwd — a relative path would
    silently read/write the wrong tree, so refuse it outright.
    """
    if path and (path.startswith("/") or path.startswith("\\")):
        return path
    if len(path) >= 3 and path[1] == ":" and path[2] in ("/", "\\"):
        return path
    raise ValueError(f"patch path {path!r} must be absolute")


def _write_verified(client, *, remote: str, path: str, data: bytes) -> None:
    client.file_write(
        remote=remote, path=path,
        content_b64=base64.b64encode(data).decode("ascii"),
    )
    written = _file_bytes(client.file_read(remote=remote, path=path, timeout=180.0))
    if written != data:
        raise ValueError(f"remote write verification failed for {path!r}")


def _apply_codex_patch(remote: str, patch_text: str) -> list:
    ops = _parse_codex_patch(patch_text)
    c = _client()
    results = []
    for op in ops:
        kind = op["kind"]
        path = _resolve_patch_path(op["path"])
        if kind == "add":
            content = op["content"]
            _write_verified(c, remote=remote, path=path, data=content.encode("utf-8"))
            results.append(f"added: {path}")
        elif kind == "delete":
            c.file_delete(remote=remote, path=path, recursive=False)
            results.append(f"deleted: {path}")
        elif kind == "update":
            res = c.file_read(remote=remote, path=path, timeout=180.0)
            orig = _file_bytes(res).decode("utf-8", "replace")
            new = _apply_hunks(orig, op["hunks"])
            _write_verified(c, remote=remote, path=path, data=new.encode("utf-8"))
            results.append(f"updated: {path}")
    return results


def _parse_codex_patch(text: str) -> list:
    """Parse ``*** Begin Patch ... *** End Patch`` into a list of ops.

    Supports ``*** Add File``, ``*** Update File``, ``*** Delete File``.
    Update hunks: ``@@`` is a hunk delimiter (not content); ``+`` add,
    ``-`` remove, any other line is context.

    Raises ``ValueError`` with a self-explanatory message when the input is
    not a well-formed patch (missing ``*** Begin Patch`` / ``*** End Patch``
    markers, or no recognizable file operations) instead of silently returning
    ``[]`` — an empty result made callers conclude the tool was broken rather
    than that their patch format was wrong.
    """
    ops = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n and not lines[i].startswith("*** Begin Patch"):
        i += 1
    if i >= n:
        raise ValueError(
            "patch is missing the '*** Begin Patch' marker; wrap the whole patch as:\n"
            "*** Begin Patch\n*** Update File: /abs/path/file.py\n@@ ...\n-old\n+new\n*** End Patch"
        )
    i += 1

    cur = None
    cur_add_lines = []
    cur_hunks = []
    cur_block = []

    def flush_block():
        nonlocal cur_block
        if cur_block and cur is not None and cur["kind"] == "update":
            cur_hunks.append({"lines": list(cur_block)})
        cur_block = []

    def flush_op():
        nonlocal cur, cur_add_lines, cur_hunks, cur_block
        if cur is None:
            return
        flush_block()
        path = cur.get("path", "")
        if not path:
            raise ValueError(
                f"{cur['kind'].capitalize()} File operation is missing a path; "
                f"use '*** {cur['kind'].capitalize()} File: /absolute/path'"
            )
        if cur["kind"] == "add":
            cur["content"] = "\n".join(cur_add_lines)
        elif cur["kind"] == "update":
            if not cur_hunks:
                raise ValueError(
                    f"Update File {path!r} contains no hunks; add an '@@' hunk "
                    "with '-' removals and/or '+' additions"
                )
            if not any(
                line.startswith("@@")
                for hunk in cur_hunks
                for line in hunk["lines"]
            ):
                raise ValueError(
                    f"Update File {path!r} contains no hunks; each edit block must start with '@@'"
                )
            if not any(
                line.startswith(("+", "-"))
                for hunk in cur_hunks
                for line in hunk["lines"]
                if not line.startswith("@@")
            ):
                raise ValueError(
                    f"Update File {path!r} contains no edits; each '@@' hunk "
                    "needs at least one '-' removal or '+' addition"
                )
            cur["hunks"] = list(cur_hunks)
        ops.append(cur)
        cur = None
        cur_add_lines = []
        cur_hunks = []
        cur_block = []

    found_end = False
    while i < n:
        line = lines[i]
        if line.startswith("*** End Patch"):
            found_end = True
            flush_op()
            break
        if line.startswith("*** Add File"):
            if not line.startswith("*** Add File:"):
                raise ValueError("malformed Add File operation; expected '*** Add File: /absolute/path'")
            flush_op()
            cur = {"kind": "add", "path": line[len("*** Add File:"):].strip()}
            cur_add_lines = []
        elif line.startswith("*** Update File"):
            if not line.startswith("*** Update File:"):
                raise ValueError("malformed Update File operation; expected '*** Update File: /absolute/path'")
            flush_op()
            cur = {"kind": "update", "path": line[len("*** Update File:"):].strip()}
            cur_hunks = []
            cur_block = []
        elif line.startswith("*** Delete File"):
            if not line.startswith("*** Delete File:"):
                raise ValueError("malformed Delete File operation; expected '*** Delete File: /absolute/path'")
            flush_op()
            cur = {"kind": "delete", "path": line[len("*** Delete File:"):].strip()}
        elif line.startswith("*** "):
            if line.strip() != "*** End of File":
                raise ValueError(
                    f"unsupported patch marker {line!r}; expected Add/Update/Delete File or '*** End of File'"
                )
        elif cur is None:
            pass
        elif cur["kind"] == "add":
            cur_add_lines.append(line[1:] if line.startswith("+") else line)
        elif cur["kind"] == "update":
            if line.startswith("@@"):
                flush_block()
                cur_block = [line]
            elif line.startswith("+") or line.startswith("-") or line.startswith(" "):
                cur_block.append(line)
            else:
                cur_block.append(" " + line)
        i += 1
    flush_op()
    if not found_end:
        raise ValueError("patch is missing the '*** End Patch' marker (truncated input?)")
    if not ops:
        raise ValueError(
            "patch contains no file operations; expected at least one "
            "'*** Add File: <path>' / '*** Update File: <path>' / "
            "'*** Delete File: <path>' line between the Begin/End markers"
        )
    return ops


# Skip the whitespace-mismatch scan above this file size. The scan is O(file
# bytes) and only runs on the failure path, so it is cheap for normal source
# files (~10 ms for 10k lines), but on pathological multi-megabyte files the
# extra pass isn't worth it just to polish an error message.
_WS_DIAG_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def _describe_context_mismatch(text: str, search_lines: list[str]) -> str:
    """Explain a 'context not found' failure.

    The most common agent error is a leading/trailing whitespace slip when
    copying a context line (e.g. the file has ``# ---`` at column 0 but the
    patch wrote `` # ---``). Detect that case and name the exact line so the
    agent can fix the indentation instead of guessing.
    """
    if len(text) > _WS_DIAG_MAX_BYTES:
        return "patch context not found"
    file_by_stripped: dict[str, str] = {}
    for fl in text.splitlines():
        s = fl.strip()
        if s:
            file_by_stripped.setdefault(s, fl)
    for i, ctx in enumerate(search_lines):
        s = ctx.strip()
        if not s:
            continue
        if s in file_by_stripped and file_by_stripped[s] != ctx:
            return (
                f"patch context not found: whitespace mismatch on context line {i + 1}. "
                f"Patch line: {ctx!r}; file line: {file_by_stripped[s]!r}. "
                "Copy the context byte-for-byte (leading/trailing spaces count)."
            )
    return "patch context not found"


def _apply_hunks(orig: str, hunks: list) -> str:
    """Apply codex-style hunks by building search/replace blocks per hunk."""
    text = orig
    for hunk in hunks:
        search = []
        replace = []
        changed = False
        for hl in hunk["lines"]:
            if hl.startswith("@@"):
                continue  # hunk delimiter, not file content
            elif hl.startswith("+"):
                replace.append(hl[1:])
                changed = True
            elif hl.startswith("-"):
                search.append(hl[1:])
                changed = True
            elif hl.startswith(" "):
                search.append(hl[1:])
                replace.append(hl[1:])
            else:
                search.append(hl)
                replace.append(hl)
        if not changed:
            continue
        search_str = "\n".join(search)
        replace_str = "\n".join(replace)
        if search_str:
            if search_str not in text:
                raise ValueError(_describe_context_mismatch(text, search))
            text = text.replace(search_str, replace_str, 1)
        elif replace_str:
            text = text.rstrip("\n") + "\n" + replace_str
    return text


# ---------------------------------------------------------------------------
# MCP tool registry
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "remote_exec_command",
        "description": "Runs a shell command, returning clean output and an exit code. One-shot by default: the command is uploaded to a temp script and its output and exit code are read back from files, on every platform. On a Windows cmd remote that script is a .cmd file, so write for-loop variables with a doubled percent (for /l %%i in (1,1,3) do ...). Pass interactive=true to keep a live PTY session for remote_write_stdin. `output` never comes back blank: a command that printed nothing yields '<aimux>command output is empty</aimux>', and a wait that expired before the command finished reports it: '<aimux>command timeout after N seconds</aimux>' when nothing had arrived yet, or '<aimux>get some output, but command timeout after N seconds</aimux>' appended to the partial output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to execute."},
                "interactive": {"type": "boolean", "default": False, "description": "True keeps a live PTY session for remote_write_stdin (REPL, pager, password prompt, heredoc). False (default) runs one-shot via a temp script and returns clean output + exit code — including on Windows, where cmd uses a .cmd temp script."},
                "login": {"type": "boolean", "default": True, "description": "True runs the shell with -l/-i semantics; false disables them (uses --norc --noprofile). Defaults to true."},
                "max_output_tokens": {"type": "number", "default": 10000, "description": "Output token budget (ceil(UTF-8 bytes / 4)). Defaults to 10000."},
                "shell": {"type": "string", "description": "Shell binary to launch. Defaults to the user's default shell."},
                "workdir": {"type": "string", "description": "Working directory for the command. Defaults to the turn cwd."},
                "yield_time_ms": {"type": "number", "default": 10000, "description": "Wait before yielding output. Defaults to 10000 ms; effective range 250-300000 ms."},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "remote_write_stdin",
        "description": "Writes characters to an existing unified exec session and returns recent output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chars": {"type": "string", "description": "Characters to write to stdin."},
                "append_enter": {"type": "boolean", "description": "Append Enter after chars. Defaults to true for command lines; common pager/control keys (q, Ctrl-C, Ctrl-D, Escape) are sent raw."},
                "max_output_tokens": {"type": "number", "default": 10000, "description": "Output token budget. Defaults to 10000."},
                "session_id": {"type": "number", "description": "Identifier of the running unified exec session. A poll that expires before the command finishes reports it in output ('<aimux>command timeout after N seconds</aimux>', or '<aimux>get some output, but command timeout after N seconds</aimux>' when partial output exists); a session that is gone returns exit_code -1."},
                "yield_time_ms": {"type": "number", "description": "Wait before yielding output. Non-empty writes default to 250 ms; empty polls wait 5000+ ms; capped at 300000 ms (5 min)."},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "remote_apply_patch",
        "description": (
            "Apply a patch to edit files on the remote machine. "
            "FREEFORM: pass the raw patch text as `input`; do NOT wrap it in JSON. "
            "Patch shape: `*** Begin Patch\\n*** Update File: /abs/path/file.py\\n@@ context line\\n-old line\\n+new line\\n*** End Patch`. "
            "Supported ops: `*** Add File:` / `*** Update File:` / `*** Delete File:`. "
            "Paths must be ABSOLUTE (a relative path raises 'patch path must be absolute'). "
            "Inside each `@@ ...` hunk, lines beginning with a single leading space are context (anchor — must match the file byte-for-byte, including blank lines and indentation); "
            "`-` lines are removed; `+` lines are inserted. "
            "Rules: (1) one edit site per `@@` hunk — never bundle multiple distant locations into one giant hunk; "
            "(2) context lines are matched strictly — do not add or omit leading/trailing spaces (an extra or missing space raises 'patch context not found'); "
            "(3) prefer a few small hunks over one big one. "
            "remote_apply_patch uses strict matching — be careful not to add or omit any space."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Raw patch text (*** Begin Patch ... *** End Patch). Paths must be absolute; context lines must match the file verbatim; one edit site per @@ hunk."},
            },
            "required": ["input"],
        },
    },
    {
        "name": "remote_view_image",
        "description": "Read an image file from the remote machine and return it as a data URL for visual inspection.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "detail": {"type": "string", "enum": ["high", "original"], "default": "high", "description": "Image detail level. Defaults to high; use original to preserve exact resolution."},
                "path": {"type": "string", "description": "Remote file path to the image."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "remote_download_file",
        "description": "Download a file from the remote machine to the local filesystem for further processing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Remote file path to download."},
                "local_path": {"type": "string", "description": "Local destination path. Defaults to the remote file's basename in the current working directory."},
                "overwrite": {"type": "boolean", "default": False, "description": "Replace an existing local file. Default false refuses to overwrite."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "remote_ping",
        "description": "Health check.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_TOOL_FUNCS = {
    "remote_exec_command": _remote_exec_command,
    "remote_write_stdin": _remote_write_stdin,
    "remote_apply_patch": _remote_apply_patch,
    "remote_view_image": _remote_view_image,
    "remote_download_file": _remote_download_file,
    "remote_ping": _remote_ping,
}


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 over stdio
# ---------------------------------------------------------------------------

PROTOCOL_VERSION = "2024-11-05"


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _result(req_id, result):
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code, message):
    _send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _dispatch(remote: str, profile: str, req: dict) -> None:
    method = req.get("method")
    req_id = req.get("id")
    if method == "initialize":
        _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": "aimux-bridge", "version": __version__},
            "capabilities": {"tools": {}},
        })
        return
    if method == "notifications/initialized":
        return  # notification — no response
    if method == "tools/list":
        _result(req_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        func = _TOOL_FUNCS.get(name)
        if func is None:
            _error(req_id, -32601, f"unknown tool: {name}")
            return
        try:
            res = func(remote, profile, args)
            _result(req_id, {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]})
        except Exception as e:  # tool execution error → MCP-level error result
            _result(req_id, {"isError": True, "content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}]})
        return
    _error(req_id, -32601, f"method not found: {method}")


def run_stdio(*, remote: str, profile: str | None = None) -> None:
    """Run the MCP stdio server bound to one bridge remote."""
    ctl = _client()
    info = ctl.get_remote(remote)
    if not info:
        print(f"aimux-mcp: remote {remote!r} not found in bridge registry", file=sys.stderr)
        sys.exit(2)
    prof = profile or info.get("default_profile")
    if not prof:
        print(f"aimux-mcp: remote {remote!r} has no default_profile; pass --profile", file=sys.stderr)
        sys.exit(2)

    global _reap_client, _remote_platform
    _reap_client = ctl
    _remote_platform = str(info.get("platform") or "")
    _request_parent_death_signal()
    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except json.JSONDecodeError:
                _error(None, -32700, "parse error")
                continue
            try:
                _dispatch(remote, prof, req)
            except BrokenPipeError:
                raise  # agent closed stdout pipe mid-write; fall through to reap
            except Exception as e:
                print(f"aimux-mcp: dispatch error: {e}", file=sys.stderr)
    except BrokenPipeError:
        pass  # agent gone; let the finally reap and exit
    finally:
        # Reap on every exit path: stdin EOF (agent exited — the primary case,
        # since the kernel closes the pipe write end on any agent death), stdout
        # BrokenPipeError, and KeyboardInterrupt (SIGINT, caught by cli.py).
        try:
            n = _reap_all_sessions(ctl, timeout=5.0)
            if n:
                print(f"aimux-mcp: reaped {n} exec session(s) on exit", file=sys.stderr)
        except Exception as e:
            print(f"aimux-mcp: reap on exit failed: {e}", file=sys.stderr)
