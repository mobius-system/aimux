"""aimux MCP server — expose one bridge remote as MCP tools over stdio.

Codex-facing tools (7, aligned with codex native tool specs):
  remote_exec_command  Run a command (one-shot temp-script on every platform; interactive=true keeps a live PTY); return clean output + exit code, or session_id. Never use it to create or edit files — use remote_apply_patch.
  remote_set_remote_workdir  Set the default working directory for this session's commands (scoped to the mobius session and bridge client).
  remote_write_stdin   Write chars to an existing exec session, return output.
  remote_apply_patch   Apply a patch (FREEFORM input).
  remote_view_image    Return a remote image file as a data URL.
  remote_download_file Download a remote file to the local filesystem.
  remote_ping          Report the remote's OS, shell, working directory, and link health.

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio).
Zero external dependencies — Python standard library only. The server binds
to a single bridge remote (``--remote <name>``); tools do not expose the
remote parameter. Logs go to stderr only (stdout is the protocol).
"""
from __future__ import annotations

import base64
import hashlib
import importlib.resources
import pathlib
import json
import os
import re
import signal
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from . import bridge
from .gui_help_document import HELP_DOCUMENT
from .gui_helper import REQUEST_TIMEOUTS as _GUI_TIMEOUTS

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

# ---------------------------------------------------------------------------
# default workdir per (mobius session × bridge client)
#
# An agent can pre-set the directory its commands run in with
# remote_set_remote_workdir instead of repeating workdir on every call. The
# setting is keyed by the pair (mobius session, bridge client identifier):
# another session's default never applies here, and this session talking to a
# different client gets that client's own default.
#
# With a mobius session id (mobius passes --mobius-session) the map is persisted
# so it survives an MCP restart inside the same session. Without one there is
# nothing to key on but this process, so the setting lives in memory only and
# the tool reports scope="process".
# ---------------------------------------------------------------------------

# Set from --mobius-session; '' when mobius did not pass one (unit tests, manual use).
_mobius_session = ""
# In-process default, used when there is no session id to key the store with.
_workdir_default = ""

_WORKDIR_STORE = Path.home() / ".aimux" / "mcp_workdirs.json"
_WORKDIR_TTL_S = 7 * 24 * 3600
_WORKDIR_CACHE: dict[str, dict] | None = None

# Absolute on POSIX (`/x`) or Windows (`C:\x`, `C:/x`, `\\host\share`).
_ABS_PATH_RE = re.compile(r"^(/|[A-Za-z]:[\\/]|\\\\)")


def _workdir_scope() -> str:
    """How long the setting lasts: the mobius session, or just this process."""
    return "session" if _mobius_session else "process"


def _workdir_key(remote: str) -> str:
    return f"{_mobius_session}|{remote}"


def _load_workdir_store() -> dict[str, dict]:
    """Read the persisted map, dropping malformed and stale entries."""
    global _WORKDIR_CACHE
    if _WORKDIR_CACHE is not None:
        return _WORKDIR_CACHE
    data: dict[str, dict] = {}
    try:
        raw = json.loads(_WORKDIR_STORE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    except Exception:
        raw = {}  # a corrupt store must not take the tool down
    if isinstance(raw, dict):
        now = time.time()
        for key, value in raw.items():
            if not isinstance(value, dict) or not value.get("workdir"):
                continue
            try:
                fresh = now - float(value.get("updated_at") or 0) < _WORKDIR_TTL_S
            except (TypeError, ValueError):
                fresh = False
            if fresh:
                data[str(key)] = value
    _WORKDIR_CACHE = data
    return data


def _save_workdir_store() -> None:
    """Write the map atomically (tmp + rename), owner-readable only."""
    if _WORKDIR_CACHE is None:
        return
    try:
        _WORKDIR_STORE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _WORKDIR_STORE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_WORKDIR_CACHE, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, _WORKDIR_STORE)
    except Exception as e:
        print(f"aimux-mcp: cannot persist workdir default: {e}", file=sys.stderr)


def _session_workdir_default(remote: str) -> str:
    """The default set for (this mobius session × this bridge client), if any."""
    if not _mobius_session:
        return _workdir_default
    entry = _load_workdir_store().get(_workdir_key(remote)) or {}
    return str(entry.get("workdir") or "")


def _effective_workdir(args: dict, remote: str) -> tuple[str | None, str]:
    """Resolve where a command should run: (workdir, source).

    Explicit argument wins outright; then the default set for this session and
    client; then nothing, which leaves the command in whatever directory the
    remote's reverse-connect client process was started in.
    """
    explicit = str(args.get("workdir") or "").strip()
    if explicit:
        return explicit, "explicit"
    saved = _session_workdir_default(remote)
    if saved:
        return saved, "session_default"
    return None, "client_cwd"

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


def _decorate_output(output: str, *, finished: bool, timeout_ms: int | None = None, dead: bool = False) -> str:
    """Make a result self-describing when it has nothing to show.

    An empty ``output`` is ambiguous: the command may have printed nothing and
    finished, the wait may have expired, or the session may have been killed
    before it printed. All three read as "no result", so say which one happened
    — and never drop what did arrive: a wait that ended with partial output
    keeps it and states the outcome after it.

    Markers are added *after* truncation so a long partial output cannot push
    the status line off the end.
    """
    if dead:
        status = _DEAD_NOTE
    elif not finished:
        status = f"command timeout after {_seconds_label(timeout_ms)} seconds"
    elif output.strip():
        return output
    else:
        return "<aimux>command output is empty</aimux>"
    if not output.strip():
        return f"<aimux>{status}</aimux>"
    return output.rstrip("\n") + "\n" + f"<aimux>get some output, but {status}</aimux>"


def _exec_result(sid: int, c, output: str, original_tokens: int, exit_code, finished: bool, wall: float,
                 *, note: str | None = None, timeout_ms: int | None = None, dead: bool = False) -> dict:
    sess = _sessions.get(sid) or {}
    res = {
        "output": _decorate_output(output, finished=finished, timeout_ms=timeout_ms, dead=dead),
        "original_token_count": original_tokens,
        "wall_time_seconds": round(wall, 3),
        # Where the command actually ran, and why -- so a default picked up from
        # remote_set_remote_workdir is visible instead of silently applied.
        "workdir": sess.get("workdir") or None,
        "workdir_source": sess.get("workdir_source"),
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

# aimux keeps its scratch files in a directory of its own instead of scattering
# them across the system temp dir: they are easy to spot, easy to sweep, and they
# never collide with another tool's names. The client creates the directory when
# it writes the script (parents=True), so the launcher's redirects have somewhere
# to land.
_POSIX_TEMP_DIR = "/tmp/aimux_tmp"
_WIN_TEMP_SUBDIR = "aimux_tmp"

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

    POSIX remotes use /tmp/aimux_tmp/<base>.{sh,out,code}. Windows remotes cannot
    use /tmp at all: only the MSYS shells have one, and the bridge client
    resolves it against the current drive, so the MCP server and the shell would
    silently disagree about which file they mean. They use
    ``~/AppData/Local/Temp/aimux_tmp`` instead — the same directory in every
    Windows shell (%USERPROFILE%, $env:USERPROFILE, $HOME).
    """
    base = _exec_temp_base()
    if not windows:
        return (f"{_POSIX_TEMP_DIR}/{base}.sh",
                f"{_POSIX_TEMP_DIR}/{base}.out",
                f"{_POSIX_TEMP_DIR}/{base}.code")
    ext = {"cmd": "cmd", "powershell": "ps1"}.get(shell, "sh")
    root = f"~/{_WIN_TEMP_DIR}/{_WIN_TEMP_SUBDIR}"
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
    win_dir = f"$HOME/{_WIN_TEMP_DIR}/{_WIN_TEMP_SUBDIR}"
    launch = (f"bash \"{win_dir}/{_basename(script)}\" > \"{win_dir}/{_basename(outfile)}\" 2>&1; "
              f"echo $? > \"{win_dir}/{_basename(codefile)}\"")
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
    workdir, workdir_source = _effective_workdir(args, remote)
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
        _sessions[sid].update(workdir=workdir or "", workdir_source=workdir_source)
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
                                note=_DEAD_NOTE if dead else None, timeout_ms=yield_ms, dead=dead)
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
    workdir, workdir_source = _effective_workdir(args, remote)
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
        _sessions[sid].update(workdir=workdir or "", workdir_source=workdir_source)
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
                                note=_DEAD_NOTE if dead else None, timeout_ms=yield_ms, dead=dead)
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
# remote_set_remote_workdir
# ---------------------------------------------------------------------------


def _remote_set_remote_workdir(remote: str, profile: str, args: dict) -> dict:
    """Set or clear the directory later commands run in.

    Validated here, at set time, so a typo surfaces now rather than on the next
    command: the directory must exist on the remote (or be created on request)
    and be a directory. Passing an empty workdir clears the default.
    """
    global _workdir_default
    raw = args.get("workdir")
    workdir = "" if raw is None else str(raw).strip()
    create = bool(args.get("create", False))
    c = _client()

    if not workdir:
        if _mobius_session:
            store = _load_workdir_store()
            if store.pop(_workdir_key(remote), None) is not None:
                _save_workdir_store()
        _workdir_default = ""
        return {
            "ok": True, "scope": _workdir_scope(), "remote": remote,
            "workdir": None, "verified": "cleared",
        }

    if not _ABS_PATH_RE.match(workdir):
        raise ValueError(
            f"workdir must be an absolute path on the remote (got {workdir!r}); "
            "a relative path would be resolved against the remote client's own directory"
        )

    stat = c.file_stat(remote=remote, path=workdir, timeout=30.0)
    if stat.get("exists"):
        if stat.get("type") != "dir":
            raise ValueError(f"workdir is not a directory on {remote}: {workdir}")
        verified = "dir"
    elif create:
        c.file_mkdir(remote=remote, path=workdir)
        verified = "created"
    else:
        raise ValueError(
            f"workdir does not exist on {remote}: {workdir} (pass create=true to create it)"
        )

    if _mobius_session:
        store = _load_workdir_store()
        store[_workdir_key(remote)] = {"workdir": workdir, "updated_at": time.time()}
        _save_workdir_store()
    else:
        _workdir_default = workdir
    return {
        "ok": True, "scope": _workdir_scope(), "remote": remote,
        "workdir": workdir, "verified": verified,
    }


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
                "output": _decorate_output("", finished=True, dead=True),
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
                        note=_DEAD_NOTE if dead else None, timeout_ms=yield_ms, dead=dead)


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


# What is worth telling the agent about the remote machine. The reverse client
# reports more than this; the rest is bookkeeping it has no use for, and every
# extra field is context spent on every ping.
_PING_SYS_FIELDS = (
    "os", "os_name", "os_release", "os_version", "os_detail", "arch",
    "hostname", "user", "home", "cwd", "path_style", "path_separator",
    "encoding", "python", "aimux_version", "time", "timezone",
)


def _shell_syntax(profile: str, windows: bool) -> str:
    """How a command must be written for the bound shell, in one phrase."""
    family = _shell_family(profile)
    if family == "cmd":
        return "cmd.exe syntax: backslash paths, %VAR%, '&' to chain"
    if family == "powershell":
        return "PowerShell syntax: backslash paths, $env:VAR, ';' to chain"
    if windows:
        return "POSIX syntax in an MSYS/Git-Bash shell, but Windows-visible paths"
    return "POSIX syntax: forward-slash paths, $VAR, ';' to chain"


def _ping_summary(remote: str, system: dict, session: dict, live: bool) -> str:
    """The one line that answers 'where am I' without reading the rest."""
    head = " ".join(
        str(part) for part in (
            system.get("os_name") or system.get("os") or "unknown OS",
            system.get("os_release"),
            system.get("arch"),
        ) if part
    )
    clauses = [head]
    who = "@".join(str(part) for part in (system.get("user"), system.get("hostname")) if part)
    if who:
        clauses.append(who)
    clauses.append(f"commands run in the {session['profile']!r} shell — {session['shell_syntax']}")
    start = session.get("workdir") or system.get("cwd")
    if start:
        clauses.append(f"they start in {start}")
    if not live:
        clauses.append("(from the bridge registry; the remote did not answer a live probe)")
    return f"remote {remote}: " + "; ".join(clauses)


def _remote_ping(remote: str, profile: str, args: dict) -> dict:
    """Report the machine this server drives — not just that it answers.

    The agent sees a bridge, not a computer, so until now it had to run
    ``hostname``/``uname`` to find out where its commands land. This reports
    what command syntax depends on: the remote's OS, the shell those commands
    run in, the directory they start from, the encoding they print in. The
    broker's own health comes along, so one call answers both "is the link
    alive" and "what is on the other end of it".

    The live report is best-effort: a client built before this request existed,
    or one that just went away, still gets described from the bridge registry.
    """
    c = _client()
    report: dict[str, Any] = {"ok": True, "remote": remote}

    try:
        report["broker"] = c.health(timeout=5.0)
    except Exception as e:
        report["ok"] = False
        report["broker"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # Read the registry row even when the live probe works: it is what a client
    # that cannot answer can still be described by.
    try:
        registered = c.get_remote(remote) or {}
    except Exception:
        registered = {}

    notes: list[str] = []
    live: dict[str, Any] = {}
    try:
        live = c.system_info(remote=remote, timeout=15.0)
    except Exception as e:
        notes.append(
            f"the remote did not answer the system-info request ({type(e).__name__}: {e}); "
            "everything below comes from the bridge registry, so the shell, the paths and the "
            "working directory are unknown (the client needs a recent aimux and a live connection)"
        )

    system: dict[str, Any] = {}
    for key in _PING_SYS_FIELDS:
        value = live.get(key)
        if value not in (None, ""):
            system[key] = value
    if not system.get("os") and registered.get("platform"):
        system["os"] = registered["platform"]
    if not system.get("aimux_version") and registered.get("client_version"):
        system["aimux_version"] = registered["client_version"]

    workdir, workdir_source = _effective_workdir({}, remote)
    windows = _remote_is_windows(profile)
    session = {
        "profile": profile,
        "shell_family": _shell_family(profile),
        "remote_is_windows": windows,
        "shell_syntax": _shell_syntax(profile, windows),
        "workdir": workdir,
        "workdir_source": workdir_source if workdir else None,
        "toolset": _ACTIVE_TOOLSET,
    }
    if workdir is None:
        notes.append(
            "no workdir default is set for this session, so commands start in "
            f"{system.get('cwd') or 'whatever directory the reverse-connect client was started in'}"
        )

    report["live"] = bool(live)
    report["system"] = system
    report["shells"] = live.get("shells") or registered.get("profiles") or []
    report["session"] = session
    report["summary"] = _ping_summary(remote, system, session, bool(live))
    if notes:
        report["notes"] = notes
    return report


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


def _patch_context_error(exc: ValueError) -> ValueError:
    """Add the recovery step to a context-mismatch error.

    Context is matched byte-for-byte, so a failed hunk is only fixed by reading
    the file again and rebuilding it. Say which tool to use — otherwise agents
    retry the same patch unchanged — and keep the original diagnosis intact.
    """
    message = str(exc)
    if "patch context not found" not in message or "remote_exec_command" in message:
        return exc
    hint = "read this file again with remote_exec_command"
    if message.endswith((".", "!", "?")):
        return ValueError(f"{message} You should {hint} and retry with the exact context.")
    return ValueError(f"{message}, you should {hint}!")


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
            try:
                new = _apply_hunks(orig, op["hunks"])
            except ValueError as exc:
                raise _patch_context_error(exc) from None
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

CLASSIC_TOOLS = [
    {
        "name": "remote_exec_command",
        "description": "Runs a shell command, returning clean output and an exit code. Create or modify files only with remote_apply_patch (reviewable diff): do NOT write file content through this tool — not via redirection, heredocs, `python -c`, `Set-Content`, `tee`, or base64/gzip/hex payloads, even when a command line is too long. Reading and deleting files here is fine. One-shot by default: the command is uploaded to a temp script and its output and exit code are read back from files, on every platform. On a Windows cmd remote that script is a .cmd file, so write for-loop variables with a doubled percent (for /l %%i in (1,1,3) do ...). Pass interactive=true to keep a live PTY session for remote_write_stdin. Set a default with remote_set_remote_workdir to stop repeating workdir on every call. `output` never comes back blank: a command that printed nothing yields '<aimux>command output is empty</aimux>', and a wait that expired before the command finished reports it: '<aimux>command timeout after N seconds</aimux>' when nothing had arrived yet, or '<aimux>get some output, but command timeout after N seconds</aimux>' appended to the partial output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to execute. Reading and deleting files is fine; create or modify files with remote_apply_patch."},
                "interactive": {"type": "boolean", "default": False, "description": "True keeps a live PTY session for remote_write_stdin (REPL, pager, password prompt, heredoc). False (default) runs one-shot via a temp script and returns clean output + exit code — including on Windows, where cmd uses a .cmd temp script."},
                "login": {"type": "boolean", "default": True, "description": "True runs the shell with -l/-i semantics; false disables them (uses --norc --noprofile). Defaults to true."},
                "max_output_tokens": {"type": "number", "default": 10000, "description": "Output token budget (ceil(UTF-8 bytes / 4)). Defaults to 10000."},
                "shell": {"type": "string", "description": "Shell binary to launch. Defaults to the user's default shell."},
                "workdir": {"type": "string", "description": "Working directory for the command, as an absolute path on the remote. A path that does not exist fails the call instead of falling back. When omitted, the command runs in the default set for this session by remote_set_remote_workdir, or -- if none was set -- wherever the remote's reverse-connect client process happens to be started, which is not knowable in advance. The reply reports both the directory used and where it came from."},
                "yield_time_ms": {"type": "number", "default": 10000, "description": "Wait before yielding output. Defaults to 10000 ms; effective range 250-300000 ms."},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "remote_set_remote_workdir",
        "description": (
            "Sets the working directory that this session's remote_exec_command calls use when they omit "
            "workdir, so it does not have to be repeated on every call. Scoped to (this mobius session, this "
            "bridge client): other sessions and other clients are unaffected. The directory is validated on "
            "the remote when set -- it must already be a directory unless create=true. Pass workdir='' to "
            "clear the default; the reply reports the scope and what was verified. Each remote_exec_command "
            "reply echoes the effective workdir and whether it came from the argument, this default, or the "
            "client's own directory."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workdir": {"type": "string", "description": "Absolute directory on the remote to use as the default working directory for this session. Pass an empty string to clear it."},
                "create": {"type": "boolean", "default": False, "description": "Create the directory when it does not exist yet. Defaults to false, in which case a missing directory is an error."},
            },
            "required": ["workdir"],
        },
    },
    {
        "name": "remote_write_stdin",
        "description": "Writes characters to an existing unified exec session and returns recent output. Create or modify files with remote_apply_patch, not by typing content here.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "chars": {"type": "string", "description": "Characters to write to stdin."},
                "append_enter": {"type": "boolean", "description": "Append Enter after chars. Defaults to true for command lines; common pager/control keys (q, Ctrl-C, Ctrl-D, Escape) are sent raw."},
                "max_output_tokens": {"type": "number", "default": 10000, "description": "Output token budget. Defaults to 10000."},
                "session_id": {"type": "number", "description": "Identifier of the running unified exec session. A poll that expires before the command finishes reports it in output ('<aimux>command timeout after N seconds</aimux>', or '<aimux>get some output, but command timeout after N seconds</aimux>' when partial output exists); a session that is gone returns exit_code -1 with '<aimux>session ended (shell exited)</aimux>' in output."},
                "yield_time_ms": {"type": "number", "description": "Wait before yielding output. Non-empty writes default to 250 ms; empty polls wait 5000+ ms; capped at 300000 ms (5 min)."},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "remote_apply_patch",
        "description": (
            "The ONLY way to create or modify files on the remote — never write file content via remote_exec_command. Deleting files with remote_exec_command is allowed. "
            "FREEFORM: pass the raw patch text as `input`; do NOT wrap it in JSON. "
            "Patch shape: `*** Begin Patch\\n*** Update File: /abs/path/file.py\\n@@ context line\\n-old line\\n+new line\\n*** End Patch`. "
            "Supported ops: `*** Add File:` / `*** Update File:` / `*** Delete File:`. "
            "Paths must be ABSOLUTE (a relative path raises 'patch path must be absolute'). "
            "Inside each `@@ ...` hunk, lines beginning with a single leading space are context (anchor — must match the file byte-for-byte, including blank lines and indentation); "
            "`-` lines are removed; `+` lines are inserted. "
            "Rules: (1) one edit site per `@@` hunk — never bundle multiple distant locations into one giant hunk; "
            "(2) context lines are matched strictly — do not add or omit leading/trailing spaces (an extra or missing space raises 'patch context not found'); "
            "if that happens, read the file again with remote_exec_command and rebuild the hunk from what is actually there instead of retrying the same patch; "
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
        "description": (
            "Report where your commands actually run: the remote machine's OS and architecture, "
            "the shell they run in and the syntax it expects, the working directory they start "
            "from, and the link's health. Call it before writing platform-specific commands "
            "(paths, quoting, which tools exist) instead of probing with `hostname`/`uname`."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_TOOL_FUNCS = {
    "remote_exec_command": _remote_exec_command,
    "remote_set_remote_workdir": _remote_set_remote_workdir,
    "remote_write_stdin": _remote_write_stdin,
    "remote_apply_patch": _remote_apply_patch,
    "remote_view_image": _remote_view_image,
    "remote_download_file": _remote_download_file,
    "remote_ping": _remote_ping,
}

TOOLSETS = ("classic", "classic-and-gui")


# ---------------------------------------------------------------------------
# remote_gui_* — computer-use tools backed by the reverse client's GUI helper
# ---------------------------------------------------------------------------

_GUI_COMMON = (
    " Start with remote_gui_diagnostics: it reports whether the helper is usable "
    "(accessibility + screen recording) and returns `skill_path`, the absolute path of "
    "the GUI-operation guide shipped with aimux -- read that SKILL.md before driving a "
    "GUI, it carries the two working methods and their pitfalls. "
    " The remote must be connected with `aimux reverse connect --enable-gui` "
    "from a logged-in desktop session (not over SSH). "
    "Refs look like `win:look_1:@e2`; a ref from a recent look can be re-resolved "
    "even after the window moves (refound, not stale). Menus and many popups are "
    "SEPARATE roots: after opening one, list windows again and look at the new root "
    "instead of re-reading the original window."
)


def _gui_call(remote: str, cmd: str, args: dict, *, timeout: float | None = None) -> dict:
    budget = timeout if timeout is not None else _GUI_TIMEOUTS.get(cmd, 45.0)
    # Broker-side timeout must exceed the helper budget: the broker would
    # otherwise give up while the helper is still legitimately working.
    return _client().gui_request(remote=remote, cmd=cmd, args=args, timeout=budget + 15.0)


def gui_skill_path() -> str | None:
    """Absolute path of the GUI-operation guide that ships inside the package.

    The guide is package data, not a file next to the caller: an MCP server can
    run anywhere, so the path has to come from the installed distribution.
    Returns None when the file is absent (a hand-copied install), in which case
    diagnostics simply omits the field.
    """
    try:
        path = pathlib.Path(str(importlib.resources.files("aimux").joinpath(
            "skills", "mobius-gui-operation", "SKILL.md")))
    except Exception:
        return None
    return str(path) if path.is_file() else None


def _gui_display_help_document(remote: str, profile: str, args: dict) -> dict:
    """Return the bundled GUI guide; nothing is sent to the remote."""
    path = pathlib.Path(__file__).with_name("gui_help_document.py")
    return {"document": HELP_DOCUMENT, "path": str(path)}


def _gui_diagnostics(remote: str, profile: str, args: dict) -> dict:
    res = _gui_call(remote, "diagnostics", {})
    skill = gui_skill_path()
    return {**res, "skill_path": skill} if skill else res


def _gui_list_windows(remote: str, profile: str, args: dict) -> dict:
    call_args: dict = {}
    if args.get("pid") is not None:
        call_args["pid"] = int(args["pid"])
    res = _gui_call(remote, "listRoots", call_args)
    roots = res.get("roots") if isinstance(res.get("roots"), list) else []
    limit = 100
    summary = []
    for w in roots[:limit]:
        summary.append(
            {
                "pid": w.get("pid"),
                "title": w.get("title"),
                "kind": w.get("kind"),
                "processName": w.get("processName"),
                "rootRef": w.get("rootRef"),
                "windowId": w.get("windowId"),
                "isFocused": w.get("isFocused"),
                "isMain": w.get("isMain"),
                "isMinimized": w.get("isMinimized"),
            }
        )
    return {"count": len(roots), "roots": summary, "truncated": len(roots) > limit}


# Screenshots arrive base64-encoded inside the look payload. Inlining them as a
# data URL puts ~90 KB of text into a single tool result, which overruns host
# token limits and buries the outline behind it. Spool to a temp file instead
# and hand back the path: the caller reads the image only when it wants to.
_GUI_SHOT_TTL_SECONDS = 6 * 60 * 60
_GUI_SHOT_MAX_FILES = 400


def _gui_shot_dir() -> Path:
    """Per-user spool directory. gettempdir() honours TMPDIR/TEMP/TMP."""
    directory = Path(tempfile.gettempdir()) / "aimux-gui-shots"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def _prune_gui_spool(directory: Path, pattern: str, ttl: float, max_files: int) -> None:
    """Drop expired files, then the oldest ones past the count cap."""
    now = time.time()
    fresh: list[tuple[float, Path]] = []
    for path in directory.glob(pattern):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if now - mtime > ttl:
            try:
                path.unlink()
            except OSError:
                pass
            continue
        fresh.append((mtime, path))
    for _, path in sorted(fresh)[: max(0, len(fresh) - max_files)]:
        try:
            path.unlink()
        except OSError:
            pass


def _prune_gui_shots(directory: Path) -> None:
    _prune_gui_spool(directory, "*.png", _GUI_SHOT_TTL_SECONDS, _GUI_SHOT_MAX_FILES)


def _spool_gui_shot(remote: str, look_id: str, b64: str) -> str:
    """Write one screenshot to a temp PNG and return its absolute path."""
    directory = _gui_shot_dir()
    _prune_gui_shots(directory)
    # Two agents bound to the same remote both issue look_1..., so disambiguate
    # with the pid of this MCP server process.
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{remote}-{look_id}")[:120] or "shot"
    path = directory / f"{stem}-{os.getpid()}.png"
    path.write_bytes(base64.b64decode(b64))
    return str(path)


def _gui_look(remote: str, profile: str, args: dict) -> dict:
    root_ref = args.get("root_ref") or args.get("rootRef")
    if not isinstance(root_ref, str) or not root_ref:
        raise ValueError("root_ref is required (get one from remote_gui_list_windows)")
    call_args: dict = {
        "rootRef": root_ref,
        "includeImage": bool(args.get("include_image", True)),
        "maxDimension": int(args.get("max_dimension", 1024) or 1024),
    }
    res = _gui_call(remote, "look", call_args)
    image_path = ""
    image = res.get("image") if isinstance(res.get("image"), dict) else None
    if image and image.get("jpegBase64"):
        # Field is historical: the payload is PNG despite the name.
        image_path = _spool_gui_shot(remote, str(res.get("lookId") or "look"), image["jpegBase64"])
    out = {
        "lookId": res.get("lookId"),
        "window": res.get("window"),
        "outline": res.get("outline"),
        "timings": res.get("timings"),
    }
    if image_path:
        out["image_path"] = image_path
    # The token budget guards the OUTLINE. The screenshot no longer competes
    # with it: it is spooled to a file, so the outline is the only text here.
    budget = int(args.get("max_output_tokens", 20000))
    outline_text, est = _truncate_output(json.dumps(res.get("outline"), ensure_ascii=False), budget)
    if est >= budget:
        return {"lookId": res.get("lookId"), "window": res.get("window"), "timings": res.get("timings"),
                "image_path": image_path,
                "outline_truncated": True,
                "error": "outline exceeded max_output_tokens; re-run with a higher budget"}
    out["estimated_tokens"] = est
    return out


def _gui_act(remote: str, profile: str, args: dict) -> dict:
    look_id = args.get("look_id")
    if not isinstance(look_id, str) or not look_id:
        raise ValueError("look_id is required (from a previous remote_gui_look)")
    action = args.get("action")
    if not isinstance(action, str) or not action:
        raise ValueError("action is required (press, click, setText, scroll, ...)")
    policy = args.get("policy") or "ax_only"
    if policy not in ("ax_only", "default", "foreground"):
        raise ValueError("policy must be ax_only, default, or foreground")
    ref = args.get("ref")
    x = args.get("x")
    y = args.get("y")
    if isinstance(ref, str) and ref:
        target: dict = {"ref": ref}
    elif x is not None and y is not None:
        target = {"x": float(x), "y": float(y)}
    else:
        raise ValueError("target needs ref (preferred, from the look outline) or x/y image coordinates")
    params: dict = {}
    for src_key, dst_key in (
        ("text", "text"),
        ("scroll_x", "scrollX"),
        ("scroll_y", "scrollY"),
        ("keys", "keys"),
        ("keep_pressdown_ms", "keepPressdownMs"),
        ("path", "path"),
    ):
        if args.get(src_key) is not None:
            params[dst_key] = args[src_key]
    if args.get("preserve_focus") is not None:
        params["preserveFocus"] = bool(args["preserve_focus"])
    call_args = {
        "lookId": look_id,
        "action": action,
        "policy": policy,
        "target": target,
        "params": params,
    }
    if args.get("defer_root_delta") is not None:
        call_args["deferRootDelta"] = bool(args["defer_root_delta"])
    return _gui_call(remote, "act", call_args)


def _gui_read_text(remote: str, profile: str, args: dict) -> dict:
    look_id = args.get("look_id")
    ref = args.get("ref")
    if not isinstance(look_id, str) or not look_id:
        raise ValueError("look_id is required")
    if not isinstance(ref, str) or not ref:
        raise ValueError("ref is required")
    call_args: dict = {"lookId": look_id, "elementRef": ref}
    if args.get("offset") is not None:
        call_args["offset"] = int(args["offset"])
    if args.get("limit") is not None:
        call_args["limit"] = int(args["limit"])
    return _gui_call(remote, "axReadText", call_args)


def _gui_wait_for(remote: str, profile: str, args: dict) -> dict:
    if not any(args.get(k) for k in ("text", "role", "value")):
        raise ValueError("wait_for requires at least one of text, role, or value")
    call_args: dict = {}
    mapping = (
        ("text", "text"),
        ("role", "role"),
        ("value", "value"),
        ("gone", "gone"),
        ("timeout_ms", "timeoutMs"),
        ("scope_exact", "scopeExact"),
        ("look_id", "lookId"),
        ("scope_ref", "scopeRef"),
        ("window_id", "windowId"),
        ("root_ref", "rootRef"),
        ("pid", "pid"),
    )
    for src_key, dst_key in mapping:
        if args.get(src_key) is not None:
            call_args[dst_key] = args[src_key]
    timeout_ms = int(call_args.get("timeoutMs", 10000) or 10000)
    return _gui_call(remote, "axWaitFor", call_args, timeout=timeout_ms / 1000.0 + 15.0)


def _gui_focus_window(remote: str, profile: str, args: dict) -> dict:
    root_ref = args.get("root_ref") or args.get("rootRef")
    if not isinstance(root_ref, str) or not root_ref:
        raise ValueError("root_ref is required")
    return _gui_call(remote, "focusWindow", {"rootRef": root_ref})


# ---------------------------------------------------------------------------
# remote_gui_record_window_mp4
# ---------------------------------------------------------------------------

# Recordings are two orders of magnitude larger than screenshots, so they get
# their own spool directory and a much smaller count cap: a minute of a small
# window is tens of megabytes, and keeping 400 of them would fill a disk.
_GUI_VIDEO_TTL_SECONDS = 6 * 60 * 60
_GUI_VIDEO_MAX_FILES = 40
_GUI_VIDEO_STREAM_THRESHOLD = 4 * 1024 * 1024
_GUI_RECORD_MIN_MS = 1000
_GUI_RECORD_MAX_MS = 600_000
_GUI_RECORD_DEFAULT_MS = 10_000
_GUI_RECORD_POLL_SECONDS = 2.0


def _gui_video_dir() -> Path:
    """Per-user spool directory for downloaded recordings."""
    directory = Path(tempfile.gettempdir()) / "aimux-gui-videos"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory


def _record_local_path(remote: str, recording_id: str, raw: object) -> Path:
    if raw is not None:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("local_path must be a non-empty string when provided")
        path = Path(raw.strip()).expanduser().resolve()
        if not path.name or path.is_dir():
            raise ValueError(f"local_path resolves to a directory: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    directory = _gui_video_dir()
    _prune_gui_spool(directory, "*.mp4", _GUI_VIDEO_TTL_SECONDS, _GUI_VIDEO_MAX_FILES)
    stem = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{remote}-{recording_id}")[:120] or "recording"
    return directory / f"{stem}-{os.getpid()}.mp4"


def _fetch_recording(remote: str, remote_path: str, local: Path) -> bytes:
    """Pull the MP4 off the remote, streaming it when it is not small."""
    size = 0
    try:
        stat = _client().file_stat(remote=remote, path=remote_path)
        size = int(stat.get("size") or 0)
    except Exception:
        size = 0  # a stat failure is not fatal; read() either works or does not
    if size > _GUI_VIDEO_STREAM_THRESHOLD:
        raw = _client().file_read_stream(remote=remote, path=remote_path, total=size)
    else:
        raw = _file_bytes(_client().file_read(remote=remote, path=remote_path))
    local.write_bytes(raw)
    return raw


def _gui_record_window_mp4(remote: str, profile: str, args: dict) -> dict:
    root_ref = args.get("root_ref") or args.get("rootRef")
    if not isinstance(root_ref, str) or not root_ref:
        raise ValueError("root_ref is required (get one from remote_gui_list_windows)")
    # The helper answers unsupported_platform too, but failing here keeps a
    # non-Windows remote from paying for a round trip to be told so.
    if not _remote_is_windows(profile):
        raise RuntimeError(
            "remote_gui_record_window_mp4 is only supported on Windows: it encodes through the "
            f"Windows Media Foundation sink writer, and remote {remote!r} reports platform "
            f"{_remote_platform or 'unknown'}"
        )
    duration_ms = args.get("duration_ms", args.get("durationMs"))
    try:
        duration_ms = int(duration_ms) if duration_ms is not None else _GUI_RECORD_DEFAULT_MS
    except (TypeError, ValueError):
        raise ValueError("duration_ms must be a number") from None
    duration_ms = max(_GUI_RECORD_MIN_MS, min(_GUI_RECORD_MAX_MS, duration_ms))
    audio = str(args.get("audio") or "system")
    if audio not in ("system", "mic", "none"):
        raise ValueError("audio must be one of: system, mic, none")
    # `fps` is the name 0.3.70 shipped with and still works; `record_fps` is the
    # one to use, so that a rate is not confused with the frames a screenshot
    # has. The helper clamps the value into the range it will encode.
    fps_raw = args.get("record_fps")
    if fps_raw is None:
        fps_raw = args.get("fps")
    try:
        fps = int(fps_raw) if fps_raw is not None else 10
    except (TypeError, ValueError):
        raise ValueError("record_fps must be a number") from None
    # 0 (or leaving it out) means "record at the window's own size"; the helper
    # clamps anything else into 64-8192.
    max_dimension_raw = args.get("max_dimension")
    if max_dimension_raw is None:
        max_dimension_raw = args.get("maxDimension")
    max_dimension = 0
    if max_dimension_raw is not None:
        try:
            max_dimension = int(max_dimension_raw)
        except (TypeError, ValueError):
            raise ValueError("max_dimension must be a number") from None
        if max_dimension < 0:
            raise ValueError("max_dimension cannot be negative")

    start = _gui_call(remote, "recordStart", {
        "rootRef": root_ref,
        "fps": fps,
        "maxDurationMs": duration_ms,
        "maxDimension": max_dimension,
        "audio": audio,
    })
    recording_id = str(start.get("recordingId") or "")
    if not recording_id:
        raise RuntimeError(f"the recording helper did not return a recordingId: {start}")

    # The tool blocks for the requested stretch, so poll instead of sleeping
    # through it: a recording that ends by itself (window closed, or the
    # max-duration cap) should not keep the caller waiting for the remainder.
    deadline = time.monotonic() + duration_ms / 1000.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_GUI_RECORD_POLL_SECONDS, remaining))
        if time.monotonic() >= deadline:
            break
        try:
            status = _gui_call(remote, "recordStatus", {"recordingId": recording_id})
        except Exception:
            break  # stop anyway; recordStop reports the real state
        if not status.get("active"):
            break

    stop = _gui_call(remote, "recordStop", {"recordingId": recording_id})
    error = str(stop.get("error") or "")
    if error and not stop.get("bytes"):
        raise RuntimeError(f"recording failed: {error}")
    remote_path = str(stop.get("path") or start.get("path") or "")
    if not remote_path:
        raise RuntimeError(f"the recording helper did not report a file path: {stop}")

    local_raw = args.get("local_path")
    local = _record_local_path(remote, recording_id, local_raw)
    if local.exists() and not args.get("overwrite"):
        raise FileExistsError(f"local file already exists (pass overwrite=true to replace): {local}")
    raw = _fetch_recording(remote, remote_path, local)

    out = {
        "video_path": str(local),
        "size": len(raw),
        "recording_id": recording_id,
        "remote_path": remote_path,
        "duration_ms": stop.get("durationMs"),
        "width": stop.get("width") or start.get("width"),
        "height": stop.get("height") or start.get("height"),
        "fps": stop.get("fps") or start.get("fps"),
        # What the helper actually applied, so a clamped cap is visible; null
        # means the video was encoded at the window's own size.
        "max_dimension": start.get("maxDimension"),
        "audio": stop.get("audio") or start.get("audio"),
        "has_audio": bool(stop.get("hasAudio", start.get("hasAudio"))),
        "video_frames": stop.get("videoFrames"),
        "audio_frames": stop.get("audioFrames"),
    }
    warning = stop.get("audioWarning") or start.get("audioWarning")
    if warning:
        out["audio_warning"] = warning
    if error:
        out["error"] = error
    return out


GUI_TOOLS = [
    {
        "name": "remote_gui_diagnostics",
        "description": "Check the remote's GUI accessibility helper: protocol version, OS, arch, helper pid, "
        "and `skill_path`, the absolute path of the bundled GUI-operation guide (SKILL.md) to read before driving a GUI."
        + _GUI_COMMON,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "remote_gui_display_help_document",
        "description": "Return the bundled GUI help document: prerequisites, the full remote_gui_* tool table, "
        "the recommended list_windows -> look -> act workflow, and the common pitfalls. Read it before starting "
        "GUI work. The document is local; no remote call is made. It complements the SKILL.md at `skill_path`: "
        "the SKILL.md covers the two working methods, this document is the tool-by-tool reference."
        + _GUI_COMMON,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "remote_gui_list_windows",
        "description": "List top-level windows on the remote desktop (pid, title, kind, rootRef, focus state). "
        "rootRef is the handle for remote_gui_look. Windows owned by elevated processes cannot be driven "
        "unless this client itself is elevated (Windows UIPI)."
        + _GUI_COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "number", "description": "Only list windows of this process id."},
            },
        },
    },
    {
        "name": "remote_gui_look",
        "description": "Observe one window atomically: accessibility outline tree (refs, roles, text, rects) plus a "
        "screenshot spooled to a local temp file whose path is returned as image_path (open it with your image "
        "reader; it is not inlined, so the tree stays readable). The outline's refs are what remote_gui_act targets. "
        "Element rects are in image pixels. Menus/popups open as NEW roots — look at those, not the original window."
        + _GUI_COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_ref": {"type": "string", "description": "Root handle from remote_gui_list_windows."},
                "include_image": {"type": "boolean", "default": True, "description": "Attach a screenshot data URL."},
                "max_dimension": {"type": "number", "default": 1024, "description": "Max screenshot dimension in px."},
                "max_output_tokens": {"type": "number", "default": 20000, "description": "Token budget for the outline."},
            },
            "required": ["root_ref"],
        },
    },
    {
        "name": "remote_gui_act",
        "description": "Act on a UI element from a previous look. Prefer ref targets: press/click drive UIA patterns "
        "(Invoke/Toggle/SelectionItem/ExpandCollapse) and setText writes through ValuePattern — semantic delivery, "
        "no real mouse/keyboard. Coordinates and typeText/keypress/drag are raw input and need policy=foreground. "
        "Returns outcome (worked/didnt/unknown), performed.delivery (ax=semantic, hid=raw input), evidence, and a "
        "rootDelta (appeared/disappeared roots — check it after menu clicks). Coordinate acts preflight occlusion "
        "and fail with occluded_target instead of blind-clicking."
        + _GUI_COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {
                "look_id": {"type": "string", "description": "lookId of the look the ref came from."},
                "action": {"type": "string", "description": "press, click, setText, scroll, typeText, keypress, drag, moveMouse."},
                "ref": {"type": "string", "description": "Element ref from the look outline (preferred target)."},
                "x": {"type": "number", "description": "Image x coordinate (only with y; raw-input path)."},
                "y": {"type": "number", "description": "Image y coordinate (only with x; raw-input path)."},
                "policy": {"type": "string", "enum": ["ax_only", "default", "foreground"], "default": "ax_only",
                            "description": "ax_only (default) never injects real input; foreground allows raw input on the user's desktop."},
                "text": {"type": "string", "description": "Text for setText/typeText."},
                "keys": {"type": "array", "items": {"type": "string"},
                          "description": "Key names for keypress, pressed in order (e.g. ['F1'], ['ctrl','c']). "
                                         "Raw input: needs policy=foreground."},
                "keep_pressdown_ms": {"type": "number", "default": 0,
                                       "description": "Hold the key down this many milliseconds before releasing it "
                                                      "(keypress only, capped at 5000). 0 releases inside one input batch, "
                                                      "which per-frame pollers such as games never observe — pass 50-150 "
                                                      "there. Costs that much latency per press. Raw input: needs "
                                                      "policy=foreground."},
                "path": {"type": "array", "items": {"type": "object"},
                          "description": "Waypoints for drag in image coordinates ([{x, y}, ...], at least two). "
                                         "Raw input: needs policy=foreground."},
                "scroll_x": {"type": "number", "description": "Horizontal scroll amount."},
                "scroll_y": {"type": "number", "description": "Vertical scroll amount."},
                "preserve_focus": {"type": "boolean", "description": "Raw-input acts: do not force the window to foreground first."},
            },
            "required": ["look_id", "action"],
        },
    },
    {
        "name": "remote_gui_read_text",
        "description": "Read the live text of one element (TextPattern -> ValuePattern -> name), faster and cheaper "
        "than a fresh look. Secure (password) fields are refused."
        + _GUI_COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {
                "look_id": {"type": "string"},
                "ref": {"type": "string"},
                "offset": {"type": "number", "default": 0},
                "limit": {"type": "number", "default": 4096},
            },
            "required": ["look_id", "ref"],
        },
    },
    {
        "name": "remote_gui_wait_for",
        "description": "Poll the remote's live accessibility tree until an element matching text/role/value appears "
        "(or disappears with gone=true). Scoped to a window via root_ref/window_id/pid, or an exact subtree via "
        "look_id+scope_ref."
        + _GUI_COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Case-insensitive substring to match."},
                "role": {"type": "string", "description": "Element role to match."},
                "value": {"type": "string", "description": "Element value to match."},
                "gone": {"type": "boolean", "default": False, "description": "Wait for the match to disappear instead."},
                "timeout_ms": {"type": "number", "default": 10000, "description": "Poll budget, 100-60000."},
                "root_ref": {"type": "string"},
                "window_id": {"type": "number"},
                "pid": {"type": "number"},
                "look_id": {"type": "string"},
                "scope_ref": {"type": "string"},
                "scope_exact": {"type": "boolean", "default": False,
                                 "description": "Match only the scope_ref element itself, not its descendants."},
            },
        },
    },
    {
        "name": "remote_gui_focus_window",
        "description": "Bring one window to the foreground."
        + _GUI_COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {"root_ref": {"type": "string"}},
            "required": ["root_ref"],
        },
    },
    {
        "name": "remote_gui_record_window_mp4",
        "description": "Record one window of the remote desktop to an MP4 (H.264 video, AAC audio) and copy it "
        "back to this machine, returning the local path as video_path. Captured through the same path as "
        "remote_gui_look, so occlusion does not matter, but the window must stay alive for the whole recording: "
        "closing it ends the recording early. Windows-only — any other platform fails immediately without "
        "recording anything. The call blocks for duration_ms and then returns; it is not a background recorder, "
        "and while it blocks the other tools of this server wait. Sound is taken from the desktop speakers "
        "(audio='system', the default), the microphone (audio='mic') or not recorded at all ('none'); if the "
        "audio device cannot be opened the video is still recorded and audio_warning explains why. The size of "
        "the file is controlled by record_fps and max_dimension."
        + _GUI_COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_ref": {"type": "string", "description": "Root handle from remote_gui_list_windows."},
                "duration_ms": {"type": "number", "default": 10000,
                                 "description": "How long to record, in ms (1000-600000). A recording that is "
                                                "cut short reports a smaller duration_ms in the result."},
                "record_fps": {"type": "number", "default": 10,
                                "description": "Recorded frames per second (1-30)."},
                "fps": {"type": "number", "description": "Alias of record_fps, kept for older callers."},
                "max_dimension": {"type": "number",
                                   "description": "Cap the longest side of the video, in pixels (64-8192, "
                                                  "aspect ratio kept, never enlarged). Handy for keeping the file "
                                                  "small: a 3096x1944 window at 1280 encodes as 1280x802. The "
                                                  "result's width/height are the encoded size. Omit (or 0) to "
                                                  "encode at the window's own size."},
                "local_path": {"type": "string",
                                "description": "Absolute local destination for the MP4. Default: a temp file under "
                                               "the GUI spool directory, pruned automatically."},
                "overwrite": {"type": "boolean", "default": False,
                               "description": "Replace local_path when it already exists."},
            },
            "required": ["root_ref"],
        },
    },
]

_GUI_TOOL_FUNCS = {
    "remote_gui_display_help_document": _gui_display_help_document,
    "remote_gui_diagnostics": _gui_diagnostics,
    "remote_gui_list_windows": _gui_list_windows,
    "remote_gui_look": _gui_look,
    "remote_gui_act": _gui_act,
    "remote_gui_read_text": _gui_read_text,
    "remote_gui_wait_for": _gui_wait_for,
    "remote_gui_focus_window": _gui_focus_window,
    "remote_gui_record_window_mp4": _gui_record_window_mp4,
}

_TOOL_FUNCS.update(_GUI_TOOL_FUNCS)

TOOLS = CLASSIC_TOOLS  # default toolset; run_stdio selects the final list


def tools_for(toolset: str) -> list[dict]:
    if toolset == "classic":
        return CLASSIC_TOOLS
    if toolset == "classic-and-gui":
        return CLASSIC_TOOLS + GUI_TOOLS
    raise ValueError(f"unknown toolset {toolset!r}")


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


_ACTIVE_TOOLSET = "classic"


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
        _result(req_id, {"tools": tools_for(_ACTIVE_TOOLSET)})
        return
    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        func = _TOOL_FUNCS.get(name)
        if func is not None and name not in {t["name"] for t in tools_for(_ACTIVE_TOOLSET)}:
            func = None
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


def run_stdio(*, remote: str, profile: str | None = None, toolset: str = "classic",
               mobius_session: str = "") -> None:
    """Run the MCP stdio server bound to one bridge remote.

    ``mobius_session`` is the id of the mobius session that owns this server.
    It scopes remote_set_remote_workdir's default and lets it persist across an
    MCP restart; without it the default lasts only as long as this process.
    """
    global _ACTIVE_TOOLSET, _mobius_session
    _mobius_session = (mobius_session or "").strip()
    if toolset not in TOOLSETS:
        print(f"aimux-mcp: unknown toolset {toolset!r} (choose from {', '.join(TOOLSETS)})", file=sys.stderr)
        sys.exit(2)
    _ACTIVE_TOOLSET = toolset
    ctl = _client()
    info = ctl.get_remote(remote)
    if not info:
        print(f"aimux-mcp: remote {remote!r} not found in bridge registry", file=sys.stderr)
        sys.exit(2)
    prof = profile or info.get("default_profile")
    if not prof:
        print(f"aimux-mcp: remote {remote!r} has no default_profile; pass --profile", file=sys.stderr)
        sys.exit(2)
    if toolset == "classic-and-gui":
        gui = info.get("gui") if isinstance(info.get("gui"), dict) else None
        if not gui or not gui.get("available"):
            reason = str((gui or {}).get("error") or "the remote did not report GUI capability")
            print(
                f"aimux-mcp: remote {remote!r} has GUI disabled: {reason}\n"
                f"           (start the client with `aimux reverse connect --enable-gui` on a "
                "logged-in desktop session, or serve with --toolset classic)",
                file=sys.stderr,
            )
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
