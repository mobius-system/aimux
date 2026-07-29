"""aimux MCP server — expose one bridge remote as MCP tools over stdio.

Codex-facing tools (5):
  remote_execute       Run a shell command, return output + exit_code.
  remote_read_file     Read a file (with pagination).
  remote_write_file    Write/create a file.
  remote_ping          Health check.
  remote_apply_patch   Apply a codex-style patch (FREEFORM: input = raw patch text).

Transport: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio).
Zero external dependencies — Python standard library only.

The server binds to a single bridge remote (``--remote <name>``); tools do not
expose the remote parameter. Logs go to stderr only (stdout is the protocol).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid

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
# remote_execute
# ---------------------------------------------------------------------------

_EXEC_SHELL = os.environ.get("AIMUX_MCP_SHELL") or os.environ.get("SHELL") or "/bin/bash"


def _remote_execute(remote: str, profile: str, args: dict) -> dict:
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command is required")
    timeout = float(args.get("timeout") or 30.0)
    workdir = args.get("workdir") or None
    max_tokens = int(args.get("max_output_tokens") or 0)  # 0 = unlimited
    shell_bin = args.get("shell") or _EXEC_SHELL
    login = args.get("login")
    if login is None:
        login = True
    # login=True  → normal interactive shell (loads rc → full env, but may print rc noise
    #              like nvm "Now using node" prompts);
    # login=False → --norc --noprofile (clean output, no rc noise, leaner env).
    shell_cmd = shell_bin if login else f"{shell_bin} --norc --noprofile"

    name = f"mcp-exec-{uuid.uuid4().hex[:10]}"
    marker = f"__AIMUX_EXIT_{uuid.uuid4().hex[:8]}__"
    c = _client()
    c.create_session(
        remote=remote, name=name, profile=profile, cmd=shell_cmd,
        cwd=workdir, created_at=bridge.utc_now(), created_by="aimux-mcp",
    )
    try:
        c.send_keys(remote=remote, name=name, keys=[command, "Enter"], flags=[], timeout=15.0)
        c.send_keys(remote=remote, name=name, keys=[f"echo {marker}:$?", "Enter"], flags=[], timeout=15.0)

        deadline = time.time() + max(timeout, 1.0)
        out = ""
        exit_code = None
        while True:
            time.sleep(0.3)
            cap = c.capture(remote=remote, name=name, lines=4000, ansi=False, timeout=10.0)
            out = str(cap.get("content") or "")
            if marker in out:
                tail = out.rsplit(marker, 1)[1]
                code_token = tail.lstrip(": \t\r\n").split()[0] if tail.strip() else ""
                try:
                    exit_code = int(code_token)
                except Exception:
                    exit_code = None
                out = out.rsplit(marker, 1)[0]
                break
            if time.time() >= deadline:
                break
        if max_tokens > 0:
            out, _ = _truncate_output(out, max_tokens)
        return {"output": out, "exit_code": exit_code}
    finally:
        try:
            c.kill_session(remote=remote, name=name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# remote_read_file (with pagination)
# ---------------------------------------------------------------------------


def _remote_read_file(remote: str, profile: str, args: dict) -> dict:
    path = args.get("path")
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    offset = int(args.get("offset") or 0)
    limit = int(args.get("limit") or 2000)
    if offset < 0:
        offset = 0
    if limit <= 0:
        limit = 2000

    res = _client().file_read(remote=remote, path=path, timeout=180.0)
    text = _file_bytes(res).decode("utf-8", "replace")
    # keep a trailing newline as its own empty last line for stable pagination
    trailing_nl = text.endswith("\n")
    lines = text.split("\n")
    if trailing_nl and lines and lines[-1] == "":
        lines = lines[:-1]
    total = len(lines)
    window = lines[offset:offset + limit]
    return {
        "path": path,
        "content": "\n".join(window),
        "total_lines": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
    }


# ---------------------------------------------------------------------------
# remote_write_file
# ---------------------------------------------------------------------------


def _remote_write_file(remote: str, profile: str, args: dict) -> dict:
    path = args.get("path")
    content = args.get("content")
    if not isinstance(path, str) or not path:
        raise ValueError("path is required")
    if content is None:
        raise ValueError("content is required")
    if not isinstance(content, str):
        content = str(content)
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    _client().file_write(remote=remote, path=path, content_b64=b64, timeout=180.0)
    return {"path": path, "bytes": len(content.encode("utf-8"))}


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


def _apply_codex_patch(remote: str, patch_text: str) -> list:
    ops = _parse_codex_patch(patch_text)
    c = _client()
    results = []
    for op in ops:
        kind = op["kind"]
        path = op["path"]
        if kind == "add":
            content = op["content"]
            c.file_write(
                remote=remote, path=path,
                content_b64=base64.b64encode(content.encode("utf-8")).decode("ascii"),
            )
            results.append(f"added: {path}")
        elif kind == "delete":
            c.file_delete(remote=remote, path=path, recursive=False)
            results.append(f"deleted: {path}")
        elif kind == "update":
            res = c.file_read(remote=remote, path=path, timeout=180.0)
            orig = _file_bytes(res).decode("utf-8", "replace")
            new = _apply_hunks(orig, op["hunks"])
            c.file_write(
                remote=remote, path=path,
                content_b64=base64.b64encode(new.encode("utf-8")).decode("ascii"),
            )
            results.append(f"updated: {path}")
    return results


def _parse_codex_patch(text: str) -> list:
    """Parse ``*** Begin Patch ... *** End Patch`` into a list of ops.

    Supports ``*** Add File``, ``*** Update File``, ``*** Delete File``.
    Update hunks: ``@@`` starts a context anchor; ``+`` add, ``-`` remove,
    any other line is context.
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
                continue  # '@@' is a hunk delimiter, not file content
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
        if search_str and search_str in text:
            text = text.replace(search_str, replace_str, 1)
        elif replace_str:
            text = text.rstrip("\n") + "\n" + replace_str
    return text


# ---------------------------------------------------------------------------
# MCP tool registry
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "remote_execute",
        "description": "Run a shell command on the remote workstation and return its output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute."},
                "timeout": {"type": "number", "default": 30, "description": "Max seconds to wait."},
                "workdir": {"type": "string", "description": "Working directory. Defaults to the remote login directory."},
                "max_output_tokens": {"type": "number", "default": 0, "description": "Output token budget (ceil(UTF-8 bytes / 4)). 0 = unlimited."},
                "shell": {"type": "string", "description": "Shell binary (e.g. bash, zsh). Defaults to $SHELL or /bin/bash."},
                "login": {"type": "boolean", "default": True, "description": "True loads shell rc (full env, may print rc noise like nvm prompts); false skips rc (--norc --noprofile) for clean output."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "remote_read_file",
        "description": "Read a file from the remote workstation (with pagination).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "number", "default": 0, "description": "Starting line (0-based)."},
                "limit": {"type": "number", "default": 2000, "description": "Max lines to return."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "remote_write_file",
        "description": "Write or create a file on the remote workstation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "remote_ping",
        "description": "Health check for the remote bridge connection.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "remote_apply_patch",
        "description": (
            "Use the remote_apply_patch tool to edit files on the remote workstation. "
            "This is a FREEFORM tool, so do not wrap the patch in JSON. "
            "The input is the raw codex-style patch text (*** Begin Patch ... *** End Patch)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "The raw codex-style patch text. Do not wrap in a JSON object.",
                },
            },
            "required": ["input"],
        },
    },
]

_TOOL_FUNCS = {
    "remote_execute": _remote_execute,
    "remote_read_file": _remote_read_file,
    "remote_write_file": _remote_write_file,
    "remote_ping": _remote_ping,
    "remote_apply_patch": _remote_apply_patch,
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
            "serverInfo": {"name": "aimux-bridge", "version": "0.1.16"},
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
        except Exception as e:
            print(f"aimux-mcp: dispatch error: {e}", file=sys.stderr)
