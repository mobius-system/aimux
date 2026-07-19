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

from . import bridge
from . import bridge_client
from . import bridge_server
from . import port_forward
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

bridge_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Bridge broker management.",
)
app.add_typer(bridge_app, name="bridge")

reverse_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Reverse-connect this machine to an aimux bridge broker.",
)
app.add_typer(reverse_app, name="reverse")

port_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Forward local HTTP traffic through an aimux bridge broker.",
)
app.add_typer(port_app, name="port")


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


def _exit_bridge_error(e: bridge.BridgeError, *, json_mode: bool = False) -> None:
    if json_mode:
        print_error_json(e.exit_code, e.kind, e.message)
    else:
        err(f"error: {e.message}")
    raise typer.Exit(e.exit_code)


def _bridge_client_or_none() -> bridge.BridgeControlClient | None:
    try:
        return bridge.BridgeControlClient.from_runtime()
    except bridge.BridgeError:
        return None


def _bridge_remote(name: str) -> tuple[bridge.BridgeControlClient | None, dict | None]:
    client = _bridge_client_or_none()
    if client is None:
        return None, None
    try:
        return client, client.get_remote(name, timeout=2.0)
    except bridge.BridgeError:
        return None, None


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
        status = "attached" if s.attached else "detached"
        rows.append([loc, s.transport, s.name, status, created])
    emit_table(["LOCATION", "TYPE", "NAME", "STATUS", "CREATED"], rows)


# ---------- new ----------

@app.command("new", help="Create a new session.")
def cmd_new(
    name: Annotated[str, typer.Option("--name", help="Session name.")],
    remote: Annotated[Optional[str], typer.Option("--remote", help="SSH host name from ~/.ssh/config.")] = None,
    profile: Annotated[
        Optional[str],
        typer.Option("--profile", help="Bridge remote profile, required for bridge remotes."),
    ] = None,
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

    if profile and not remote:
        err("error: --profile is only valid for bridge remotes")
        raise typer.Exit(EXIT_USER_ERROR)

    if tmux.has_session(name):
        if reuse:
            return
        err(f"error: session {name!r} already exists")
        raise typer.Exit(EXIT_USER_ERROR)

    bridge_client_ctl: bridge.BridgeControlClient | None = None
    bridge_remote_info: dict | None = None
    ssh_exists = False
    if remote:
        ssh_exists = ssh_mod.host_exists(remote)
        bridge_client_ctl, bridge_remote_info = _bridge_remote(remote)

        if bridge_remote_info is not None:
            if ssh_exists:
                err(f"error: remote {remote!r} exists as both ssh and bridge; rename one of them")
                raise typer.Exit(EXIT_USER_ERROR)
            if not profile:
                err(f"error: --profile is required for bridge remote {remote!r}")
                raise typer.Exit(EXIT_USER_ERROR)
            if output_sync_to_file:
                err("error: --output-sync-to-file is not supported for bridge remotes")
                raise typer.Exit(EXIT_USER_ERROR)
            if bridge_remote_info.get("status") != "connected":
                err(f"error: bridge remote {remote!r} is disconnected")
                raise typer.Exit(EXIT_SSH_UNREACHABLE)
            if reuse:
                try:
                    existing = bridge_client_ctl.list_sessions(timeout=2.0) if bridge_client_ctl else []
                except bridge.BridgeError:
                    existing = []
                if any(s.get("location") == remote and s.get("name") == name for s in existing):
                    return
            try:
                assert bridge_client_ctl is not None
                bridge_client_ctl.create_session(
                    remote=remote,
                    name=name,
                    profile=profile,
                    cmd=cmd,
                    cwd=cwd,
                    created_at=_now_iso(),
                    created_by=_actor(),
                )
                return
            except bridge.BridgeError as e:
                _exit_bridge_error(e)

        if profile:
            err("error: --profile is only valid for bridge remotes")
            raise typer.Exit(EXIT_USER_ERROR)
        if not ssh_exists:
            if bridge_client_ctl is None:
                err(f"error: host {remote!r} not found in ~/.ssh/config")
            else:
                err(f"error: remote {remote!r} not found in ~/.ssh/config or bridge registry")
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
    if s.backend == "bridge":
        err("error: attach does not support bridge remote sessions; use send-keys + capture")
        raise typer.Exit(EXIT_SAFETY_DENIED)
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
    if s.backend == "bridge":
        try:
            bridge.BridgeControlClient.from_runtime().send_keys(
                remote=s.location,
                name=s.name,
                keys=keys,
                flags=flags,
            )
            return
        except bridge.BridgeError as e:
            _exit_bridge_error(e)
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


@app.command("send_files", help="Upload multiple local files or directories to an SSH or bridge remote.")
def cmd_send_files(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    remote_path: Annotated[str, typer.Argument(help="Destination directory on the remote host.")],
    local_paths: Annotated[list[str], typer.Argument(help="Local files or directories to upload.")],
    gitignore: Annotated[
        bool,
        typer.Option("--gitignore", help="Skip files ignored by git ignore rules when uploading."),
    ] = False,
):
    bridge_client_ctl, bridge_info = _bridge_remote(remote)
    if bridge_client_ctl is not None and bridge_info is not None:
        try:
            result = transfer.bridge_send_files(
                bridge_client_ctl, remote, local_paths, remote_path, gitignore=gitignore
            )
        except bridge.BridgeError as e:
            _exit_bridge_error(e)
        except transfer.TransferError as e:
            err(f"error: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
        print(f"uploaded {result.uploaded_files} file(s) to bridge remote {remote}:{remote_path}")
        if result.skipped_files:
            print(f"skipped {result.skipped_files} gitignored file(s)")
        return

    if not ssh_mod.host_exists(remote):
        err(f"error: host {remote!r} not found in ~/.ssh/config or bridge registry")
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

@app.command("get_files", help="Download multiple remote files or directories from an SSH or bridge remote.")
def cmd_get_files(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    local_dir: Annotated[str, typer.Argument(help="Local destination directory.")],
    remote_paths: Annotated[list[str], typer.Argument(help="Remote files or directories to download.")],
):
    bridge_client_ctl, bridge_info = _bridge_remote(remote)
    if bridge_client_ctl is not None and bridge_info is not None:
        try:
            result = transfer.bridge_get_files(bridge_client_ctl, remote, remote_paths, local_dir)
        except bridge.BridgeError as e:
            _exit_bridge_error(e)
        except transfer.TransferError as e:
            err(f"error: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
        print(f"downloaded {result.downloaded_paths} path(s) from bridge remote {remote} to {result.plan.local_dir}")
        return

    if not ssh_mod.host_exists(remote):
        err(f"error: host {remote!r} not found in ~/.ssh/config or bridge registry")
        raise typer.Exit(EXIT_USER_ERROR)

    try:
        result = transfer.get_paths(remote, remote_paths, local_dir)
    except transfer.SftpDownloadError as e:
        _classify_and_exit_sftp(remote, "download", e)
    except transfer.TransferError as e:
        err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    print(f"downloaded {result.downloaded_paths} path(s) from {remote} to {result.plan.local_dir}")


# ---------- sync_dir ----------

def _sync_dir_progress(phase: str, payload: dict[str, object]) -> None:
    if phase == "scan_source":
        print(f"sync_dir: scanning {payload.get('location')} source {payload.get('path')}")
        return
    if phase == "scan_destination":
        print(f"sync_dir: scanning {payload.get('location')} destination {payload.get('path')}")
        return
    if phase == "plan":
        print(
            "sync_dir: plan "
            f"{payload.get('changed_files')} changed file(s), "
            f"{payload.get('deleted_paths')} delete(s), "
            f"{payload.get('dirs')} new dir(s)"
        )
        return
    if phase == "archive":
        print(
            f"sync_dir: building archive with {payload.get('files')} file(s) "
            f"and {payload.get('dirs')} dir(s)"
        )
        return
    if phase == "transfer":
        print(f"sync_dir: transferring archive ({payload.get('archive_size')} byte(s))")
        return
    if phase == "apply":
        print(f"sync_dir: applying changes on {payload.get('location')}")
        return
    if phase == "noop":
        print("sync_dir: no changes")
        return
    if phase == "done":
        print("sync_dir: done")


@app.command("sync_dir", help="Differentially synchronize a local directory with a bridge remote directory.")
def cmd_sync_dir(
    remote: Annotated[str, typer.Argument(help="Bridge remote name.")],
    remote_path: Annotated[str, typer.Argument(help="Remote directory path.")],
    local_path: Annotated[str, typer.Argument(help="Local directory path.")],
    to_remote: Annotated[
        bool,
        typer.Option("--to-remote", help="Sync LOCAL_PATH to REMOTE:REMOTE_PATH."),
    ] = False,
    from_remote: Annotated[
        bool,
        typer.Option("--from-remote", help="Sync REMOTE:REMOTE_PATH to LOCAL_PATH."),
    ] = False,
    exclude: Annotated[
        Optional[list[str]],
        typer.Option(
            "--exclude",
            help="Exclude a file or directory pattern from sync; may be used multiple times.",
        ),
    ] = None,
):
    if to_remote == from_remote:
        err("error: exactly one of --to-remote or --from-remote is required")
        raise typer.Exit(EXIT_USER_ERROR)

    bridge_client_ctl, bridge_info = _bridge_remote(remote)
    if bridge_client_ctl is None or bridge_info is None:
        err(f"error: bridge remote {remote!r} not found")
        raise typer.Exit(EXIT_USER_ERROR)
    if bridge_info.get("status") != "connected":
        err(f"error: bridge remote {remote!r} is disconnected")
        raise typer.Exit(EXIT_SSH_UNREACHABLE)

    try:
        result = transfer.bridge_sync_dir(
            bridge_client_ctl,
            remote,
            remote_path,
            local_path,
            to_remote=to_remote,
            from_remote=from_remote,
            progress=_sync_dir_progress,
            exclude=exclude or (),
        )
    except bridge.BridgeError as e:
        _exit_bridge_error(e)
    except transfer.TransferError as e:
        err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    if result.direction == "to-remote":
        print(
            f"synced {result.synced_files} file(s) to bridge remote {remote}:{remote_path}; "
            f"deleted {result.deleted_paths} path(s)"
        )
    else:
        print(
            f"synced {result.synced_files} file(s) from bridge remote {remote}:{remote_path} "
            f"to {result.local_path}; deleted {result.deleted_paths} path(s)"
        )


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
    if s.backend == "bridge":
        try:
            payload = bridge.BridgeControlClient.from_runtime().capture(
                remote=s.location,
                name=s.name,
                lines=lines,
                ansi=ansi,
            )
        except bridge.BridgeError as e:
            _exit_bridge_error(e, json_mode=json_out)
        content = str(payload.get("content") or "")
        if json_out:
            print_json(
                {
                    "id": s.id,
                    "lines": lines,
                    "content": content,
                    "ansi": bool(payload.get("ansi")),
                    "source": payload.get("source"),
                }
            )
        else:
            sys.stdout.write(content)
            if not content.endswith("\n"):
                sys.stdout.write("\n")
        return
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
    if s.backend == "bridge":
        try:
            bridge.BridgeControlClient.from_runtime().kill_session(remote=s.location, name=s.name)
            return
        except bridge.BridgeError as e:
            _exit_bridge_error(e)
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
    bridge_remotes: list[dict] = []
    bridge_client_ctl = _bridge_client_or_none()
    if bridge_client_ctl is not None:
        try:
            bridge_remotes = bridge_client_ctl.list_remotes(timeout=2.0)
        except bridge.BridgeError:
            bridge_remotes = []

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
                    "type": "ssh",
                    "user": h.user,
                    "hostname": h.hostname,
                    "port": h.port,
                    "status": r.status if r else "unknown",
                    "rtt_ms": r.rtt_ms if r else None,
                }
            )
        for remote in bridge_remotes:
            out.append(remote)
        print_json(out)
        return

    rows = []
    for h in hosts:
        r = results.get(h.name)
        rtt = f"{r.rtt_ms}ms" if r and r.rtt_ms is not None else "-"
        status = r.status if r else "unknown"
        rows.append([h.name, "ssh", h.user or "-", h.hostname or h.name, h.port, status, rtt])
    for remote in bridge_remotes:
        profiles = ",".join(
            str(p.get("name"))
            for p in remote.get("profiles", [])
            if isinstance(p, dict) and p.get("available", True)
        )
        rows.append(
            [
                remote.get("name"),
                "bridge",
                remote.get("platform") or "-",
                profiles or "-",
                "-",
                remote.get("status") or "unknown",
                "-",
            ]
        )
    emit_table(["HOST", "TYPE", "USER/PLATFORM", "HOSTNAME/PROFILES", "PORT", "STATUS", "RTT"], rows)


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
        bridge_client_ctl, remote = _bridge_remote(name)
        if remote is not None:
            status = str(remote.get("status") or "unknown")
            if json_out:
                print_json(
                    {
                        "name": name,
                        "type": "bridge",
                        "status": status,
                        "platform": remote.get("platform"),
                        "profiles": remote.get("profiles", []),
                    }
                )
            else:
                profiles = ",".join(
                    str(p.get("name"))
                    for p in remote.get("profiles", [])
                    if isinstance(p, dict) and p.get("available", True)
                )
                print(f"{name}\tbridge\t{status}\tprofiles={profiles or '-'}")
            raise typer.Exit(EXIT_OK if status == "connected" else EXIT_SSH_UNREACHABLE)
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
    _, existing_bridge = _bridge_remote(alias)
    if existing_bridge is not None:
        err(f"error: host {alias!r} conflicts with an active bridge remote; pick another --name")
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


# ---------- bridge deploy ----------

@bridge_app.command("deploy", help="Start an aimux bridge broker and listen for reverse clients.")
def cmd_bridge_deploy(
    port: Annotated[int, typer.Option("--port", help="Port to listen on.")],
    host: Annotated[str, typer.Option("--host", help="Listen address.")] = "0.0.0.0",
    token: Annotated[Optional[str], typer.Option("--token", help="Shared bridge token.")] = None,
):
    try:
        bridge_server.serve(host, port, token=token)
    except OSError as e:
        err(f"error: failed to start bridge broker: {e}")
        raise typer.Exit(EXIT_USER_ERROR)


# ---------- reverse connect ----------

@reverse_app.command("connect", help="Reverse-connect this machine to an aimux bridge broker.")
def cmd_reverse_connect(
    url: Annotated[str, typer.Argument(help="Bridge broker URL, e.g. http://server:8765.")],
    identifier: Annotated[str, typer.Option("--identifier", help="Bridge remote name to register.")],
    token: Annotated[str, typer.Option("--token", help="Shared bridge token from bridge deploy.")],
    default_profile: Annotated[
        Optional[str],
        typer.Option("--default-profile", help="Default profile reported by this client."),
    ] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Replace an existing client with the same identifier.")] = False,
):
    try:
        bridge_client.connect(
            url,
            identifier=identifier,
            token=token,
            default_profile=default_profile,
            replace=replace,
        )
    except bridge.BridgeError as e:
        _exit_bridge_error(e)
    except KeyboardInterrupt:
        raise typer.Exit(EXIT_OK)


# ---------- port forward ----------

@port_app.command("forward", help="Forward local HTTP, SSE, and WebSocket traffic through a bridge broker.")
def cmd_port_forward(
    url: Annotated[str, typer.Argument(help="Bridge broker URL, e.g. https://server/aimux_bridge.")],
    local_port: Annotated[int, typer.Option("--local-port", help="Local browser-facing port.")],
    remote_port: Annotated[int, typer.Option("--remote-port", help="Target port visible from the bridge server.")],
    token: Annotated[str, typer.Option("--token", help="Bridge token or upstream proxy JWT.")],
    local_host: Annotated[str, typer.Option("--local-host", help="Local listen address.")] = "127.0.0.1",
    remote_host: Annotated[str, typer.Option("--remote-host", help="Target host visible from the bridge server.")] = "127.0.0.1",
    remote_scheme: Annotated[str, typer.Option("--remote-scheme", help="Target scheme: http or https.")] = "http",
    preserve_host: Annotated[bool, typer.Option("--preserve-host", help="Send the browser Host header to the target.")] = False,
    rewrite_origin: Annotated[bool, typer.Option("--rewrite-origin/--no-rewrite-origin", help="Rewrite local Origin/Referer to the target origin.")] = True,
    rewrite_secure_cookie: Annotated[bool, typer.Option("--rewrite-secure-cookie", help="Remove Secure from target cookies for a local HTTP listener.")] = False,
):
    if not 0 <= local_port <= 65535:
        err("error: --local-port must be between 0 and 65535")
        raise typer.Exit(EXIT_USER_ERROR)
    if not 1 <= remote_port <= 65535:
        err("error: --remote-port must be between 1 and 65535")
        raise typer.Exit(EXIT_USER_ERROR)
    target_origin = f"{remote_scheme}://{remote_host}:{remote_port}"
    try:
        port_forward.serve_local_forward(
            url,
            local_host=local_host,
            local_port=local_port,
            target_origin=target_origin,
            token=token,
            preserve_host=preserve_host,
            rewrite_origin=rewrite_origin,
            rewrite_secure_cookie=rewrite_secure_cookie,
        )
    except bridge.BridgeError as e:
        _exit_bridge_error(e)
    except OSError as e:
        err(f"error: failed to start port forward: {e}")
        raise typer.Exit(EXIT_USER_ERROR)
    except KeyboardInterrupt:
        raise typer.Exit(EXIT_OK)
