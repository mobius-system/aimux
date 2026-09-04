"""aimux MCP server — expose one bridge remote as MCP tools over stdio.

Codex-facing tools (6, aligned with codex native tool specs):
  remote_exec_command  Run a command in a PTY; return output or session_id.
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
from pathlib import Path, PureWindowsPath

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


def _close_exec_session(sid: int, c) -> None:
    sess = _sessions.pop(sid, None)
    if not sess:
        return
    try:
        c.kill_session(remote=sess["remote"], name=sess["name"])
    except Exception:
        pass


def _reap_all_sessions(c, *, timeout: float = 5.0) -> int:
    """Kill every tracked exec session. Best-effort, idempotent, safe to call
    repeatedly (kill_session treats a missing session as success). Returns the
    count reaped. Called at process exit so the bridge PTYs this MCP server
    created do not leak on the (shared) remote client.

    Iterates ONLY this process's local ``_sessions`` — never list_sessions() /
    pattern-matches — so it cannot touch a concurrent sibling MCP process's
    sessions (mcp-exec-<uuid> names are globally unique regardless). Short
    timeout bounds exit latency if the client is down.
    """
    sids = list(_sessions.keys())
    reaped = 0
    for sid in sids:
        sess = _sessions.get(sid)
        if not sess:
            continue
        try:
            c.kill_session(remote=sess["remote"], name=sess["name"], timeout=timeout)
        except Exception as e:
            print(f"aimux-mcp: reap kill_session failed sid={sid}: {e}", file=sys.stderr)
        _sessions.pop(sid, None)
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
    """Linux: ask the kernel to deliver SIGTERM to us when our parent dies, so
    _on_sigterm runs and reaps. Closes the 'orphaned with stdin still open'
    hole the EOF reap cannot reach. No-op on macOS/Windows (EOF + the SIGTERM
    handler remain adequate there). Best-effort: a missing prctl/libc never
    blocks startup."""
    if sys.platform != "linux":
        return
    try:
        import ctypes
        # CDLL(None) resolves against the main program (which links libc),
        # robust across glibc (.so.6) and musl (libc.musl-*.so.1).
        libc = ctypes.CDLL(None, use_errno=True)
        PR_SET_PDEATHSIG = 1
        SIGTERM = 15
        libc.prctl(PR_SET_PDEATHSIG, SIGTERM, 0, 0, 0)
        # TOCTOU guard: parent may have died between fork and prctl.
        if os.getppid() == 1:
            os._exit(127)
    except Exception as e:
        print(f"aimux-mcp: PR_SET_PDEATHSIG unavailable: {e}", file=sys.stderr)


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

    Returns (output, exit_code, finished, wall_seconds).
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
    return out, exit_code, finished, time.time() - started


def _exec_result(sid: int, c, output: str, original_tokens: int, exit_code, finished: bool, wall: float) -> dict:
    res = {
        "output": output,
        "original_token_count": original_tokens,
        "wall_time_seconds": round(wall, 3),
    }
    if finished:
        if exit_code is not None:
            res["exit_code"] = exit_code
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
        # CALL triggers a second percent-expansion pass after ``cmd`` exits.
        return f"{cmd} & call echo {marker}%%errorlevel%%"
    return f"{cmd}; {marker_cmd}"


def _remote_exec_command(remote: str, profile: str, args: dict) -> dict:
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
    yield_ms = max(250, min(30000, int(args.get("yield_time_ms") or 10000)))

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
            out, exit_code, finished, wall = _drain(c, remote, name, marker, yield_ms, True)
            out_t, original = _truncate_output(out, max_tokens)
            return _exec_result(sid, c, out_t, original, exit_code, finished, wall)
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
    yield_ms = max(0, min(30000, int(args.get("yield_time_ms") or default_yield)))

    name = sess["name"]
    c = _client()
    try:
        if has_chars:
            # The original exec command owns the outstanding marker. Do not
            # enqueue another shell command here: an interactive child (pager,
            # password prompt, raw stdin reader) could consume that marker as
            # input. This tool is stdin, not command execution.
            marker = sess.get("marker", "")
            keys = [chars, "Enter"] if append_enter else [chars]
            c.send_keys(remote=remote, name=name, keys=keys, flags=[], timeout=15.0)
        else:
            # empty poll: watch the session's outstanding marker (set by exec or a
            # previous write) so we can still detect command completion.
            marker = sess.get("marker", "")
        out, exit_code, finished, wall = _drain(c, remote, name, marker, yield_ms, bool(marker))
    except bridge.BridgeError as e:
        if e.kind in ("session_gone", "not_found") and not has_chars:
            # Empty poll on a dead session: the remote shell exited (or was
            # killed). Report as finished so callers see a clean terminal state
            # instead of a cryptic error.
            _close_exec_session(sid, c)
            return {
                "output": "",
                "original_token_count": 0,
                "wall_time_seconds": 0,
                "exit_code": -1,
                "note": "session ended (shell exited)",
            }
        # Session gone mid-write or any other error: clean up and re-raise.
        _close_exec_session(sid, c)
        raise
    out_t, original = _truncate_output(out, max_tokens)
    return _exec_result(sid, c, out_t, original, exit_code, finished, wall)


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
    workdir = None
    if isinstance(args, dict):
        patch_text = args.get("input")
        if patch_text is None:
            patch_text = args.get("patch") or args.get("text") or ""
        workdir = args.get("workdir") or None
    else:
        patch_text = str(args or "")
    if not isinstance(patch_text, str) or not patch_text.strip():
        raise ValueError("input (raw patch text) is required")
    if workdir is not None and (not isinstance(workdir, str) or not workdir.strip()):
        raise ValueError("workdir must be a non-empty remote directory")
    applied = _apply_codex_patch(remote, patch_text, workdir=workdir)
    return {"applied": applied}


def _resolve_patch_path(path: str, workdir: str | None) -> str:
    """Resolve relative patch paths against the remote working directory.

    Bridge file operations run in the reverse client's process cwd, which is
    unrelated to the agent's remote session cwd. Requiring an explicit
    workdir for relative paths prevents a successful write to the wrong tree.
    """
    if not path or path.startswith("/") or path.startswith("\\"):
        return path
    if len(path) >= 3 and path[1] == ":" and path[2] in ("/", "\\"):
        return path
    if not workdir:
        raise ValueError(
            f"relative patch path {path!r} requires workdir; use an absolute remote path"
        )
    separator = "\\" if PureWindowsPath(workdir).drive else "/"
    return workdir.rstrip("/\\") + separator + path


def _write_verified(client, *, remote: str, path: str, data: bytes) -> None:
    client.file_write(
        remote=remote, path=path,
        content_b64=base64.b64encode(data).decode("ascii"),
    )
    written = _file_bytes(client.file_read(remote=remote, path=path, timeout=180.0))
    if written != data:
        raise ValueError(f"remote write verification failed for {path!r}")


def _apply_codex_patch(remote: str, patch_text: str, *, workdir: str | None = None) -> list:
    ops = _parse_codex_patch(patch_text)
    c = _client()
    results = []
    for op in ops:
        kind = op["kind"]
        path = _resolve_patch_path(op["path"], workdir)
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
    """
    ops = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n and not lines[i].startswith("*** Begin Patch"):
        i += 1
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
        if cur["kind"] == "add":
            cur["content"] = "\n".join(cur_add_lines)
        elif cur["kind"] == "update":
            cur["hunks"] = list(cur_hunks)
        ops.append(cur)
        cur = None
        cur_add_lines = []
        cur_hunks = []
        cur_block = []

    while i < n:
        line = lines[i]
        if line.startswith("*** End Patch"):
            flush_op()
            break
        if line.startswith("*** Add File: "):
            flush_op()
            cur = {"kind": "add", "path": line[len("*** Add File: "):].strip()}
            cur_add_lines = []
        elif line.startswith("*** Update File: "):
            flush_op()
            cur = {"kind": "update", "path": line[len("*** Update File: "):].strip()}
            cur_hunks = []
            cur_block = []
        elif line.startswith("*** Delete File: "):
            flush_op()
            cur = {"kind": "delete", "path": line[len("*** Delete File: "):].strip()}
        elif line.startswith("*** "):
            pass  # ignore other markers (e.g. End of File)
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
    return ops


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
                raise ValueError("patch context not found")
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
        "description": "Runs a command in a PTY, returning output or a session ID for ongoing interaction.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to execute."},
                "login": {"type": "boolean", "default": True, "description": "True runs the shell with -l/-i semantics; false disables them (uses --norc --noprofile). Defaults to true."},
                "max_output_tokens": {"type": "number", "default": 10000, "description": "Output token budget (ceil(UTF-8 bytes / 4)). Defaults to 10000."},
                "shell": {"type": "string", "description": "Shell binary to launch. Defaults to the user's default shell."},
                "workdir": {"type": "string", "description": "Working directory for the command. Defaults to the turn cwd."},
                "yield_time_ms": {"type": "number", "default": 10000, "description": "Wait before yielding output. Defaults to 10000 ms; effective range 250-30000 ms."},
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
                "session_id": {"type": "number", "description": "Identifier of the running unified exec session."},
                "yield_time_ms": {"type": "number", "description": "Wait before yielding output. Non-empty writes default to 250 ms; empty polls wait 5000+ ms."},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "remote_apply_patch",
        "description": (
            "Apply a patch to edit files on the remote machine. "
            "FREEFORM: pass the raw patch text as `input`; do NOT wrap it in JSON. "
            "Patch shape: `*** Begin Patch\\n*** Update File: path/to/file.py\\n@@ context line\\n-old line\\n+new line\\n*** End Patch`. "
            "Supported ops: `*** Add File:` / `*** Update File:` / `*** Delete File:`. "
            "Inside each `@@ ...` hunk, lines beginning with a single leading space are context (anchor — must match the file byte-for-byte, including blank lines and indentation); "
            "`-` lines are removed; `+` lines are inserted. "
            "Rules: (1) one edit site per `@@` hunk — never bundle multiple distant locations into one giant hunk; "
            "(2) context lines must be copied verbatim from the file (a mismatch raises 'patch context not found'); "
            "(3) prefer a few small hunks over one big one."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Raw patch text (*** Begin Patch ... *** End Patch). Context lines must match the file verbatim; one edit site per @@ hunk."},
                "workdir": {"type": "string", "description": "Remote working directory for relative patch paths. Prefer absolute paths when omitted."},
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

    global _reap_client
    _reap_client = ctl
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
