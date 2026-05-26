"""aimux CLI — typer-based entrypoint."""
from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from typing import Annotated

import click
import typer

from . import session as session_mod
from . import ssh as ssh_mod
from . import tmux
from . import transfer
from . import wait as wait_mod
from .constants import (
    EXIT_AUTH_REQUIRED,
    EXIT_OK,
    EXIT_SAFETY_DENIED,
    EXIT_SSH_UNREACHABLE,
    EXIT_TIMEOUT,
    EXIT_USER_ERROR,
    OPT_CREATED_AT,
    OPT_CREATED_BY,
    OPT_HOST,
    OPT_LOCATION,
    OPT_OUTPUT_SYNC_FILE,
)
from .output import emit_table, err, print_error_json, print_json

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="aimux — AI-agent-friendly tmux wrapper.",
    context_settings={"help_option_names": ["-h", "--help"]},
)

remote_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Remote-host management (reads/appends ~/.ssh/config).",
)
app.add_typer(remote_app, name="remote")


# ---------- helpers ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _actor() -> str:
    return os.environ.get("AIMUX_ACTOR") or os.environ.get("USER") or "unknown"


def _tmux_target_gone(e: tmux.TmuxError) -> bool:
    msg = e.stderr.lower()
    return "no server running" in msg or "can't find session" in msg


def _resolve_or_die(spec: str, *, json_mode: bool = False) -> session_mod.Session:
    try:
        return session_mod.resolve(spec)
    except session_mod.IdError as e:
        if json_mode:
            print_error_json(EXIT_USER_ERROR, "not_found", str(e))
        else:
            err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)


# ---------- ls ----------

@app.command("ls", help="List all aimux sessions.")
def cmd_ls(
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON.")] = False,
    remote_only: Annotated[bool, typer.Option("--remote-only", help="Only show remote sessions.")] = False,
    local_only: Annotated[bool, typer.Option("--local-only", help="Only show local sessions.")] = False,
):
    sessions = session_mod.list_sessions()
    if remote_only:
        sessions = [s for s in sessions if s.location != "local"]
    if local_only:
        sessions = [s for s in sessions if s.location == "local"]

    if json_out:
        print_json([s.to_dict() for s in sessions])
        return

    rows = []
    for s in sessions:
        loc = s.location if s.managed else f"{s.location} (unmanaged)"
        created = s.created_at or "-"
        rows.append([loc, s.name, "attached" if s.attached else "detached", created])
    emit_table(["LOCATION", "NAME", "STATUS", "CREATED"], rows)


# ---------- new ----------

@app.command("new", help="Create a new session.")
def cmd_new(
    name: Annotated[str, typer.Option("--name", help="Session name.")],
    remote: Annotated[Optional[str], typer.Option("--remote", help="SSH host name from ~/.ssh/config.")] = None,
    cwd: Annotated[Optional[str], typer.Option("--cwd", help="Initial working directory.")] = None,
    cmd: Annotated[Optional[str], typer.Option("--cmd", help="Initial command to run.")] = None,
    output_sync_to_file: Annotated[
        Optional[str],
        typer.Option("--output-sync-to-file", help="Real-time mirror pane output to this file (append)."),
    ] = None,
    reuse: Annotated[bool, typer.Option("--reuse", help="Return success if session already exists.")] = False,
):
    if "/" in name:
        err("error: --name must not contain '/'")
        raise typer.Exit(EXIT_USER_ERROR)

    if tmux.has_session(name):
        if reuse:
            return
        err(f"error: session {name!r} already exists")
        raise typer.Exit(EXIT_USER_ERROR)

    if remote and not ssh_mod.host_exists(remote):
        err(f"error: host {remote!r} not found in ~/.ssh/config")
        raise typer.Exit(EXIT_USER_ERROR)

    try:
        if remote:
            if not ssh_mod.is_resolvable(remote):
                err(f"error: ssh host {remote!r} is not resolvable via ssh -G")
                raise typer.Exit(EXIT_SSH_UNREACHABLE)
            launch = ssh_mod.build_remote_launch(remote, cwd=cwd, cmd=cmd)
            tmux.new_session(name, cwd=None, command=launch)
            location = remote
            host_val = remote
        else:
            tmux.new_session(name, cwd=cwd, command=cmd)
            location = "local"
            host_val = ""

        tmux.set_option(name, OPT_LOCATION, location)
        tmux.set_option(name, OPT_HOST, host_val)
        tmux.set_option(name, OPT_CREATED_AT, _now_iso())
        tmux.set_option(name, OPT_CREATED_BY, _actor())

        if output_sync_to_file:
            path = Path(output_sync_to_file).expanduser().resolve()
            if not path.parent.exists():
                err(f"error: parent directory does not exist: {path.parent}")
                # session was created — keep it; user can kill if they want a clean slate
                raise typer.Exit(EXIT_USER_ERROR)
            # Use absolute path quoted via shlex for the pipe command.
            import shlex
            pipe_cmd = f"cat >> {shlex.quote(str(path))}"
            tmux.pipe_pane(name, pipe_cmd)
            tmux.set_option(name, OPT_OUTPUT_SYNC_FILE, str(path))
    except tmux.TmuxError as e:
        if remote and _tmux_target_gone(e):
            err(
                f"error: remote session {remote!r}/{name!r} exited before aimux could initialize it; "
                f"run 'aimux remote test {remote}' to check SSH reachability"
            )
            raise typer.Exit(EXIT_SSH_UNREACHABLE)
        if not remote and _tmux_target_gone(e):
            err(f"error: session {name!r} exited before aimux could initialize it")
            raise typer.Exit(EXIT_USER_ERROR)
        raise


# ---------- attach ----------

@app.command("attach", help="Attach to a session. ⚠️  Human use only — agents should use send-keys + capture.")
def cmd_attach(
    target: Annotated[str, typer.Argument(help="Session id (location/name or short name).")],
    read_only: Annotated[bool, typer.Option("--read-only", help="Attach in read-only mode.")] = False,
):
    s = _resolve_or_die(target)
    tmux.attach(s.name, read_only=read_only)


# ---------- send-keys ----------

def _parse_send_keys_args(rest: list[str]) -> tuple[list[str], list[str]]:
    """Pull leading tmux flags out of `rest`; the remainder is keys.
    Supports a literal '--' separator that ends flag parsing."""
    flags: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--":
            return flags, rest[i + 1:]
        if a in ("-F", "-H", "-K", "-l", "-M", "-R", "-X"):
            flags.append(a)
            i += 1
            continue
        if a == "-N":
            if i + 1 >= len(rest):
                raise click.UsageError("-N requires a count")
            flags += ["-N", rest[i + 1]]
            i += 2
            continue
        return flags, rest[i:]
    return flags, []


@app.command(
    "send-keys",
    help=(
        "Send keys to a session. Behaves exactly like 'tmux send-keys': "
        "supports -F/-H/-K/-l/-M/-R/-X/-N <count>. Pass 'Enter' as a key to submit a line."
    ),
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True, "allow_interspersed_args": False},
)
def cmd_send_keys(
    ctx: typer.Context,
    target: Annotated[str, typer.Argument(help="Session id.")],
):
    flags, keys = _parse_send_keys_args(list(ctx.args))
    if not keys:
        err("error: send-keys requires at least one key argument")
        raise typer.Exit(EXIT_USER_ERROR)
    s = _resolve_or_die(target)
    tmux.send_keys(s.name, keys, flags=flags)


# ---------- file transfers ----------

def _classify_and_exit_sftp(remote: str, action: str, e: transfer.SftpError) -> None:
    status = ssh_mod._classify(e.returncode, "\n".join([e.stderr, e.stdout]))
    err(f"error: sftp {action} to {remote!r} failed: {e}")
    if status == "auth-required":
        raise typer.Exit(EXIT_AUTH_REQUIRED)
    if status == "unreachable":
        raise typer.Exit(EXIT_SSH_UNREACHABLE)
    raise typer.Exit(EXIT_USER_ERROR)


@app.command("send_files", help="Upload multiple local files or directories to an SSH host using sftp.")
def cmd_send_files(
    remote: Annotated[str, typer.Argument(help="SSH host name from ~/.ssh/config.")],
    remote_path: Annotated[str, typer.Argument(help="Destination directory on the remote host.")],
    local_paths: Annotated[list[str], typer.Argument(help="Local files or directories to upload.")],
    gitignore: Annotated[
        bool,
        typer.Option("--gitignore", help="Skip files ignored by git ignore rules when uploading."),
    ] = False,
):
    if not ssh_mod.host_exists(remote):
        err(f"error: host {remote!r} not found in ~/.ssh/config")
        raise typer.Exit(EXIT_USER_ERROR)

    try:
        result = transfer.send_paths(remote, local_paths, remote_path, gitignore=gitignore)
    except transfer.SftpUploadError as e:
        _classify_and_exit_sftp(remote, "upload", e)
    except transfer.TransferError as e:
        err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    print(f"uploaded {result.uploaded_files} file(s) to {remote}:{remote_path}")
    if result.skipped_files:
        print(f"skipped {result.skipped_files} gitignored file(s)")


# ---------- get_files ----------

@app.command("get_files", help="Download multiple remote files or directories from an SSH host using sftp.")
def cmd_get_files(
    remote: Annotated[str, typer.Argument(help="SSH host name from ~/.ssh/config.")],
    local_dir: Annotated[str, typer.Argument(help="Local destination directory.")],
    remote_paths: Annotated[list[str], typer.Argument(help="Remote files or directories to download.")],
):
    if not ssh_mod.host_exists(remote):
        err(f"error: host {remote!r} not found in ~/.ssh/config")
        raise typer.Exit(EXIT_USER_ERROR)

    try:
        result = transfer.get_paths(remote, remote_paths, local_dir)
    except transfer.SftpDownloadError as e:
        _classify_and_exit_sftp(remote, "download", e)
    except transfer.TransferError as e:
        err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    print(f"downloaded {result.downloaded_paths} path(s) from {remote} to {result.plan.local_dir}")


# ---------- capture ----------

@app.command("capture", help="Capture pane output (last N lines).")
def cmd_capture(
    target: Annotated[str, typer.Argument(help="Session id.")],
    lines: Annotated[int, typer.Option("--lines", help="Number of lines to capture (required, must be > 0).")] = 0,
    ansi: Annotated[bool, typer.Option("--ansi", help="Preserve ANSI escape sequences.")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON.")] = False,
):
    if lines <= 0:
        msg = "--lines must be a positive integer"
        if json_out:
            print_error_json(EXIT_USER_ERROR, "bad_argument", msg)
        else:
            err(f"error: {msg}")
        raise typer.Exit(EXIT_USER_ERROR)
    s = _resolve_or_die(target, json_mode=json_out)
    content = tmux.capture_pane(s.name, lines=lines, ansi=ansi)
    if json_out:
        print_json({"id": s.id, "lines": lines, "content": content})
    else:
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")


# ---------- kill ----------

@app.command("kill", help="Destroy a single session. Does not accept wildcards or batch.")
def cmd_kill(
    target: Annotated[str, typer.Argument(help="Session id.")],
):
    s = _resolve_or_die(target)
    tmux.kill_session(s.name)


# ---------- wait-last-command-complete ----------

@app.command(
    "wait-last-command-complete",
    help="Block until the pane's foreground process is back at a shell. Local sessions only.",
)
def cmd_wait(
    target: Annotated[str, typer.Argument(help="Session id.")],
    timeout: Annotated[str, typer.Option("--timeout", help="Required. Format: <int>s|m|h, e.g. 30s, 5m, 2h.")],
    print_lines: Annotated[
        Optional[int],
        typer.Option("--print-lines", help="After wait, print pane's last N lines on stdout."),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
    poll_interval: Annotated[str, typer.Option("--poll-interval", help="e.g. 2s")] = "2s",
):
    try:
        timeout_s = wait_mod.parse_duration(timeout)
        poll_s = wait_mod.parse_duration(poll_interval)
    except ValueError as e:
        if json_out:
            print_error_json(EXIT_USER_ERROR, "bad_argument", str(e))
        else:
            err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    if print_lines is not None and print_lines <= 0:
        msg = "--print-lines must be a positive integer"
        if json_out:
            print_error_json(EXIT_USER_ERROR, "bad_argument", msg)
        else:
            err(f"error: {msg}")
        raise typer.Exit(EXIT_USER_ERROR)

    s = _resolve_or_die(target, json_mode=json_out)
    if s.location != "local":
        msg = (
            "wait-last-command-complete only supports local sessions; "
            "for remote sessions, send-keys an explicit completion marker and poll capture"
        )
        if json_out:
            print_error_json(EXIT_SAFETY_DENIED, "remote_unsupported", msg)
        else:
            err(f"error: {msg}")
        raise typer.Exit(EXIT_SAFETY_DENIED)

    res = wait_mod.wait(s.name, timeout_s, poll_s)

    content: Optional[str] = None
    if print_lines is not None and not res.session_gone:
        try:
            content = tmux.capture_pane(s.name, lines=print_lines, ansi=False)
        except tmux.TmuxError:
            content = None

    # Outputs:
    if res.session_gone:
        if json_out:
            print_error_json(EXIT_USER_ERROR, "session_gone", f"session {s.id!r} disappeared", elapsed_ms=res.elapsed_ms)
        else:
            err(f"error: session {s.id!r} disappeared during wait")
        raise typer.Exit(EXIT_USER_ERROR)

    if json_out:
        if res.completed:
            payload = {"completed": True, "elapsed_ms": res.elapsed_ms}
            if content is not None:
                payload["content"] = content
            print_json(payload)
            raise typer.Exit(EXIT_OK)
        # timeout
        payload = {
            "error": {
                "code": EXIT_TIMEOUT,
                "kind": "timeout",
                "message": f"command did not complete within {timeout}",
                "elapsed_ms": res.elapsed_ms,
            }
        }
        if content is not None:
            payload["content"] = content
        print(__import__("json").dumps(payload, ensure_ascii=False))
        raise typer.Exit(EXIT_TIMEOUT)

    # Text mode
    status_line = "status: completed" if res.completed else "status: timeout"
    elapsed_line = f"elapsed: {res.elapsed_ms / 1000:.1f}s"
    if content is not None:
        # stdout = pane content, stderr = status
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
        err(status_line)
        err(elapsed_line)
    else:
        print(status_line)
        print(elapsed_line)
    raise typer.Exit(EXIT_OK if res.completed else EXIT_TIMEOUT)


# ---------- remote ls ----------

@remote_app.command("ls", help="List ssh-config hosts and probe reachability in parallel.")
def cmd_remote_ls(
    json_out: Annotated[bool, typer.Option("--json")] = False,
    timeout: Annotated[str, typer.Option("--timeout", help="Per-host probe timeout, e.g. 5s.")] = "5s",
):
    try:
        timeout_s = int(wait_mod.parse_duration(timeout))
    except ValueError as e:
        err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    hosts = ssh_mod.list_hosts()
    results: dict[str, ssh_mod.ProbeResult] = {}
    if hosts:
        with ThreadPoolExecutor(max_workers=min(16, len(hosts))) as pool:
            for h, r in zip(hosts, pool.map(lambda h: ssh_mod.probe(h.name, timeout_s), hosts)):
                results[h.name] = r

    if json_out:
        out = []
        for h in hosts:
            r = results.get(h.name)
            out.append(
                {
                    "name": h.name,
                    "user": h.user,
                    "hostname": h.hostname,
                    "port": h.port,
                    "status": r.status if r else "unknown",
                    "rtt_ms": r.rtt_ms if r else None,
                }
            )
        print_json(out)
        return

    rows = []
    for h in hosts:
        r = results.get(h.name)
        rtt = f"{r.rtt_ms}ms" if r and r.rtt_ms is not None else "-"
        status = r.status if r else "unknown"
        rows.append([h.name, h.user or "-", h.hostname or h.name, h.port, status, rtt])
    emit_table(["HOST", "USER", "HOSTNAME", "PORT", "STATUS", "RTT"], rows)


# ---------- remote test ----------

@remote_app.command("test", help="Probe a single ssh-config host.")
def cmd_remote_test(
    name: Annotated[str, typer.Argument(help="Host name in ~/.ssh/config.")],
    timeout: Annotated[str, typer.Option("--timeout")] = "5s",
    json_out: Annotated[bool, typer.Option("--json")] = False,
):
    try:
        timeout_s = int(wait_mod.parse_duration(timeout))
    except ValueError as e:
        err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    if not ssh_mod.host_exists(name):
        msg = f"host {name!r} not found in ~/.ssh/config"
        if json_out:
            print_error_json(EXIT_USER_ERROR, "host_not_found", msg)
        else:
            err(f"error: {msg}")
        raise typer.Exit(EXIT_USER_ERROR)

    r = ssh_mod.probe(name, timeout_s)
    if json_out:
        print_json({"name": name, "status": r.status, "rtt_ms": r.rtt_ms})
    else:
        rtt = f"{r.rtt_ms}ms" if r.rtt_ms is not None else "-"
        print(f"{name}\t{r.status}\trtt={rtt}")
        if r.status not in ("reachable", "auth-required") and r.stderr:
            err(r.stderr.strip())

    if r.status == "reachable":
        raise typer.Exit(EXIT_OK)
    if r.status == "auth-required":
        raise typer.Exit(EXIT_AUTH_REQUIRED)
    if r.status == "unreachable":
        raise typer.Exit(EXIT_SSH_UNREACHABLE)
    raise typer.Exit(EXIT_USER_ERROR)


# ---------- remote hardware ----------

@remote_app.command("hardware", help="Probe a host for GPU count/model, CPU count, and memory.")
def cmd_remote_hardware(
    name: Annotated[str, typer.Argument(help="Host name in ~/.ssh/config.")],
    timeout: Annotated[str, typer.Option("--timeout")] = "10s",
    json_out: Annotated[bool, typer.Option("--json")] = False,
):
    try:
        timeout_s = int(wait_mod.parse_duration(timeout))
    except ValueError as e:
        err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    if not ssh_mod.host_exists(name):
        msg = f"host {name!r} not found in ~/.ssh/config"
        if json_out:
            print_error_json(EXIT_USER_ERROR, "host_not_found", msg)
        else:
            err(f"error: {msg}")
        raise typer.Exit(EXIT_USER_ERROR)

    try:
        info = ssh_mod.hardware_info(name, timeout_s)
    except ssh_mod.HardwareError as e:
        msg = f"failed to gather hardware info from {name}: {e}"
        if json_out:
            print_error_json(EXIT_SSH_UNREACHABLE, "ssh_failed", msg)
        else:
            err(f"error: {msg}")
        raise typer.Exit(EXIT_SSH_UNREACHABLE)

    mem_gb: float | None = None
    if info.mem_total_kb is not None:
        mem_gb = round(info.mem_total_kb / 1024 / 1024, 2)

    if json_out:
        print_json(
            {
                "name": name,
                "gpu_count": info.gpu_count,
                "gpu_model": info.gpu_model,
                "cpu_count": info.cpu_count,
                "mem_total_kb": info.mem_total_kb,
                "mem_total_gb": mem_gb,
            }
        )
        return

    mem_str = f"{mem_gb} GB" if mem_gb is not None else "-"
    emit_table(
        ["HOST", "GPU_COUNT", "GPU_MODEL", "CPU_COUNT", "MEMORY"],
        [[name, info.gpu_count, info.gpu_model or "-", info.cpu_count if info.cpu_count is not None else "-", mem_str]],
    )


# ---------- remote add ----------

@remote_app.command("add", help="Probe a host; if reachable, append a Host block to ~/.ssh/config.")
def cmd_remote_add(
    host: Annotated[str, typer.Option("--host", help="HostName (IP or DNS).")],
    user: Annotated[str, typer.Option("--user", help="SSH user.")],
    port: Annotated[int, typer.Option("--port", help="SSH port.")] = 22,
    name: Annotated[Optional[str], typer.Option("--name", help="Alias to use in ssh config; defaults to --host.")] = None,
    identity: Annotated[Optional[str], typer.Option("--identity", help="Path to private key file.")] = None,
    timeout: Annotated[str, typer.Option("--timeout")] = "5s",
):
    try:
        timeout_s = int(wait_mod.parse_duration(timeout))
    except ValueError as e:
        err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    alias = name or host
    if ssh_mod.host_exists(alias):
        err(f"error: host {alias!r} already exists in ssh config; pick another --name")
        raise typer.Exit(EXIT_USER_ERROR)

    r = ssh_mod.probe_raw(host, port, user, identity, timeout_s)
    if r.status == "auth-required":
        err(f"error: probe to {user}@{host}:{port} got auth-required (network OK but no key auth)")
        if r.stderr:
            err(r.stderr.strip())
        raise typer.Exit(EXIT_AUTH_REQUIRED)
    if r.status != "reachable":
        err(f"error: probe to {user}@{host}:{port} -> {r.status}")
        if r.stderr:
            err(r.stderr.strip())
        raise typer.Exit(EXIT_SSH_UNREACHABLE)

    backup = ssh_mod.append_host(name=alias, host=host, port=port, user=user, identity=identity)
    rtt = f"{r.rtt_ms}ms" if r.rtt_ms is not None else "-"
    print(f"{alias}\treachable\trtt={rtt}")
    print(f"appended to ~/.ssh/config (backup: {backup})")
