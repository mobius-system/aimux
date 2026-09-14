"""aimux CLI — typer-based entrypoint."""
from __future__ import annotations

import base64
import hashlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

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

file_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "Fine-grained file operations (list/stat/read/write/mkdir/delete/rename) "
        "on an SSH or bridge remote. Auto-dispatches by remote name."
    ),
)
app.add_typer(file_app, name="file")

mcp_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Run a stdio MCP server exposing a bridge remote to external agents.",
)
app.add_typer(mcp_app, name="mcp")


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


def _add_underscore_alias(name: str) -> None:
    """Register `<name>` (e.g. send_files) as a hidden alias for the
    hyphenated command (send-files). Renamed commands keep working for
    older docs / agent muscle memory without cluttering --help."""
    import copy

    for existing in app.registered_commands:
        if getattr(existing, "name", None) == name.replace("_", "-"):
            cloned = copy.copy(existing)
            cloned.name = name
            cloned.hidden = True
            app.registered_commands.append(cloned)
            return
    raise RuntimeError(f"cannot alias {name!r}: hyphenated command not found")


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

    bridge_client_ctl: bridge.BridgeControlClient | None = None
    bridge_remote_info: dict | None = None
    ssh_exists = False
    if remote:
        # Resolve bridge remotes before checking the local tmux namespace.
        # Bridge sessions live on ``remote/name`` and may legitimately share a
        # name with a local or SSH wrapper session.
        bridge_client_ctl, bridge_remote_info = _bridge_remote(remote)

    # Local tmux is a namespace of its own.  Only check it for local sessions,
    # or for SSH remotes (which use a local tmux wrapper).  Never let a local
    # same-name session block creation on a bridge remote.
    if bridge_remote_info is None and tmux.has_session(name):
        if reuse:
            return
        err(f"error: session {name!r} already exists")
        raise typer.Exit(EXIT_USER_ERROR)

    if remote:
        ssh_exists = ssh_mod.host_exists(remote)

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


@app.command("send-files", help="Upload multiple local files or directories to an SSH or bridge remote.")
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
                bridge_client_ctl, remote, local_paths, remote_path, gitignore=gitignore,
                progress=_stream_progress(),
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


# ---------- get-files ----------

@app.command("get-files", help="Download multiple remote files or directories from an SSH or bridge remote.")
def cmd_get_files(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    local_dir: Annotated[str, typer.Argument(help="Local destination directory.")],
    remote_paths: Annotated[list[str], typer.Argument(help="Remote files or directories to download.")],
):
    bridge_client_ctl, bridge_info = _bridge_remote(remote)
    if bridge_client_ctl is not None and bridge_info is not None:
        try:
            result = transfer.bridge_get_files(bridge_client_ctl, remote, remote_paths, local_dir, progress=_stream_progress())
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


# ---------- sync-dir ----------


def _human_size(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{n}B"


def _stream_progress() -> Callable[[int, int], None]:
    """Return a progress callback that renders a single-line percentage bar.

    Only re-renders when the whole-percent value changes (or on completion),
    so a large multi-chunk transfer stays on one line.
    """
    last = [-1]

    def _cb(done: int, total: int) -> None:
        if total <= 0:
            return
        pct = int(done * 100 / total)
        if pct == last[0] and done < total:
            return
        last[0] = pct
        width = 20
        filled = int(width * done / total)
        bar = "#" * filled + "-" * (width - filled)
        if done >= total:
            sys.stdout.write(f"\r  {pct:3d}% [{bar}] {_human_size(done)}/{_human_size(total)}\n")
        else:
            sys.stdout.write(f"\r  {pct:3d}% [{bar}] {_human_size(done)}/{_human_size(total)}")
        sys.stdout.flush()

    return _cb


def _sync_dir_progress(phase: str, payload: dict[str, object]) -> None:
    if phase == "scan_source":
        print(f"sync-dir: scanning {payload.get('location')} source {payload.get('path')}")
        return
    if phase == "scan_destination":
        print(f"sync-dir: scanning {payload.get('location')} destination {payload.get('path')}")
        return
    if phase == "plan":
        print(
            "sync-dir: plan "
            f"{payload.get('changed_files')} changed file(s), "
            f"{payload.get('deleted_paths')} delete(s), "
            f"{payload.get('dirs')} new dir(s)"
        )
        return
    if phase == "archive":
        print(
            f"sync-dir: building archive with {payload.get('files')} file(s) "
            f"and {payload.get('dirs')} dir(s)"
        )
        return
    if phase == "transfer":
        print(f"sync-dir: transferring archive ({payload.get('archive_size')} byte(s))")
        return
    if phase == "apply":
        print(f"sync-dir: applying changes on {payload.get('location')}")
        return
    if phase == "noop":
        print("sync-dir: no changes")
        return
    if phase == "done":
        print("sync-dir: done")


@app.command("sync-dir", help="Differentially synchronize a local directory with a bridge remote directory.")
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


# Hidden underscore aliases for the renamed hyphenated commands (back-compat).
_add_underscore_alias("send_files")
_add_underscore_alias("get_files")
_add_underscore_alias("sync_dir")


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

def _stream_remote_event(event: str, **payload) -> None:
    """Write one immediately visible NDJSON event for incremental consumers."""
    print_json({"event": event, **payload})
    sys.stdout.flush()


def _ssh_remote_payload(h: ssh_mod.SshHost, r: ssh_mod.ProbeResult | None) -> dict:
    return {
        "name": h.name,
        "type": "ssh",
        "user": h.user,
        "hostname": h.hostname,
        "port": h.port,
        "status": r.status if r else "probing",
        "rtt_ms": r.rtt_ms if r else None,
    }


@remote_app.command("ls", help="List ssh-config hosts and probe reachability in parallel.")
def cmd_remote_ls(
    json_out: Annotated[bool, typer.Option("--json")] = False,
    stream: Annotated[
        bool,
        typer.Option("--stream", help="Emit config and probe results incrementally as NDJSON."),
    ] = False,
    timeout: Annotated[str, typer.Option("--timeout", help="Per-host probe timeout, e.g. 5s.")] = "5s",
):
    try:
        timeout_s = int(wait_mod.parse_duration(timeout))
    except ValueError as e:
        err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    hosts = ssh_mod.list_hosts()
    bridge_client_ctl = _bridge_client_or_none()

    if stream:
        _stream_remote_event("start", ssh_count=len(hosts))
        for h in hosts:
            _stream_remote_event("remote", phase="discovered", remote=_ssh_remote_payload(h, None))

        futures = {}
        bridge_count = 0
        worker_count = len(hosts) + (1 if bridge_client_ctl is not None else 0)
        if worker_count:
            with ThreadPoolExecutor(max_workers=min(17, worker_count)) as pool:
                for h in hosts:
                    futures[pool.submit(ssh_mod.probe, h.name, timeout_s)] = ("ssh", h)
                if bridge_client_ctl is not None:
                    futures[pool.submit(bridge_client_ctl.list_remotes, timeout=2.0)] = ("bridge", None)

                for future in as_completed(futures):
                    kind, host = futures[future]
                    if kind == "bridge":
                        try:
                            bridge_remotes = future.result()
                        except bridge.BridgeError:
                            bridge_remotes = []
                        bridge_count = len(bridge_remotes)
                        for remote in bridge_remotes:
                            _stream_remote_event("remote", phase="result", remote=remote)
                        continue

                    try:
                        probe_result = future.result()
                    except Exception as exc:
                        probe_result = ssh_mod.ProbeResult(
                            name=host.name,
                            status="unknown",
                            rtt_ms=None,
                            stderr=str(exc),
                        )
                    _stream_remote_event(
                        "remote",
                        phase="result",
                        remote=_ssh_remote_payload(host, probe_result),
                    )

        _stream_remote_event("done", count=len(hosts) + bridge_count)
        return

    bridge_remotes: list[dict] = []
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
            out.append(_ssh_remote_payload(h, r))
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


@bridge_app.command("restart", help="Restart the aimux bridge broker recorded in runtime.json (keeps host/port/token).")
def cmd_bridge_restart(
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds to wait for the old broker to exit.")] = 10.0,
):
    runtime = bridge.read_runtime()
    if runtime is None or runtime.pid is None:
        err("error: no running bridge broker found (runtime.json missing or has no pid); use 'aimux bridge deploy' to start one")
        raise typer.Exit(EXIT_USER_ERROR)
    if runtime.pid == os.getpid():
        err("error: refusing to restart myself")
        raise typer.Exit(EXIT_USER_ERROR)

    import signal
    import time as _time

    try:
        os.kill(runtime.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # already dead; stale runtime.json — fall through and re-serve
    except PermissionError as e:
        err(f"error: cannot signal old broker pid {runtime.pid}: {e}")
        raise typer.Exit(EXIT_USER_ERROR)

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            os.kill(runtime.pid, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.1)
    else:
        err(f"error: old broker pid {runtime.pid} did not exit within {timeout:g}s")
        raise typer.Exit(EXIT_TIMEOUT)

    # Port may need a moment to be released by the kernel after process exit.
    _time.sleep(0.2)
    try:
        bridge_server.serve(runtime.host, runtime.port, token=runtime.token)
    except OSError as e:
        err(f"error: failed to restart bridge broker on {runtime.host}:{runtime.port}: {e}")
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
    silent_shell: Annotated[bool, typer.Option("--silent-shell", help="Windows only: hide console windows of spawned shells so they run in the background.")] = False,
    silent_shell_v2: Annotated[
        bool,
        typer.Option(
            "--slient-v2",
            "--silent-v2",
            help="Windows only: run bridge shells with CREATE_NO_WINDOW and pipes (no console flash).",
        ),
    ] = False,
):
    try:
        bridge_client.connect(
            url,
            identifier=identifier,
            token=token,
            default_profile=default_profile,
            replace=replace,
            silent_shell=silent_shell,
            silent_shell_v2=silent_shell_v2,
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


# ---------- file operations (list / stat / read / write / mkdir / delete / rename) ----------
#
# Each command auto-dispatches by remote name via _bridge_remote: a registered
# bridge remote is served by BridgeControlClient.file_*; anything else must be
# an ~/.ssh/config host and is served by transfer.ssh_file_*. Mirrors the
# send_files/get_files dispatch shape.


def _parse_file_mode(mode: str) -> int:
    s = (mode or "").strip().lower()
    if s.startswith("0o"):
        s = s[2:]
    try:
        return int(s, 8)
    except ValueError:
        err(f"error: invalid file mode {mode!r}")
        raise typer.Exit(EXIT_USER_ERROR)


def _file_require_ssh(remote: str) -> None:
    if not ssh_mod.host_exists(remote):
        err(f"error: host {remote!r} not found in ~/.ssh/config or bridge registry")
        raise typer.Exit(EXIT_USER_ERROR)


def _require_absolute(path: str, *, what: str = "--path") -> str:
    """read/apply-patch only accept absolute paths: a relative remote path
    silently resolves against the reverse client's process cwd (an unrelated
    tree), so make the target explicit or refuse."""
    if path.startswith("/") or path.startswith("\\"):
        return path
    if len(path) >= 3 and path[1] == ":" and path[2] in ("/", "\\"):
        return path
    err(f"error: {what} must be an absolute path (got {path!r})")
    raise typer.Exit(EXIT_USER_ERROR)


def _detect_encoding(data: bytes) -> str:
    """Best-effort BOM/strict-decode sniffing, stdlib only (no chardet dep).

    Returns 'binary' for non-text payloads; callers render those as a byte
    summary instead of content."""
    if data.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if data.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return "utf-32"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if not data:
        return "utf-8"
    # Binary sniff: NUL bytes anywhere, or a dense run of non-printable control
    # bytes (sampled over the first 4 KiB — enough signal, cheap on big files).
    window = data[:4096]
    n_control = sum(1 for b in window if b < 9 or (13 < b < 32))
    if b"\x00" in window or n_control > len(window) * 0.30:
        return "binary"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "latin-1"


def _read_remote_bytes(remote: str, path: str) -> bytes:
    """Single bridge/SSH read funnel reused by `read` and `apply-patch`."""
    client, info = _bridge_remote(remote)
    if client is not None and info is not None:
        try:
            resp = client.file_read(remote=remote, path=path)
        except bridge.BridgeError as e:
            _exit_bridge_error(e)
        import base64 as _b64

        v = resp.get("content_b64")
        if isinstance(v, str) and v:
            return _b64.b64decode(v)
        v = resp.get("content")
        if isinstance(v, str) and v:
            try:
                return _b64.b64decode(v)
            except Exception:
                return v.encode("utf-8")
        return b""
    _file_require_ssh(remote)
    try:
        return transfer.ssh_file_read(remote, path)
    except transfer.TransferError as e:
        err(f"error: {e}")
        raise typer.Exit(EXIT_USER_ERROR)


@file_app.command("list", help="List a remote directory.")
def cmd_file_list(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    path: Annotated[str, typer.Option("--path", help="Directory path.")] = ".",
    recursive: Annotated[bool, typer.Option("--recursive", help="Recurse into subdirectories.")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="Output as JSON.")] = False,
):
    client, info = _bridge_remote(remote)
    if client is not None and info is not None:
        try:
            payload = client.file_list(remote=remote, path=path, recursive=recursive)
        except bridge.BridgeError as e:
            _exit_bridge_error(e, json_mode=json_out)
    else:
        _file_require_ssh(remote)
        try:
            payload = transfer.ssh_file_list(remote, path, recursive=recursive)
        except transfer.TransferError as e:
            err(f"error: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
    if json_out:
        print_json(payload)
        return
    entries = payload.get("entries") or []
    if not entries:
        return
    rows = []
    for e in entries:
        kind = str(e.get("type") or "?")[:1]
        size = e.get("size")
        rows.append([kind, str(size) if size else "-", str(e.get("name") or "")])
    emit_table(["T", "SIZE", "NAME"], rows)


@file_app.command("stat", help="Stat a remote path (type/size).")
def cmd_file_stat(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    path: Annotated[str, typer.Option("--path", help="Path.")],
    json_out: Annotated[bool, typer.Option("--json")] = False,
):
    client, info = _bridge_remote(remote)
    if client is not None and info is not None:
        try:
            payload = client.file_stat(remote=remote, path=path)
        except bridge.BridgeError as e:
            _exit_bridge_error(e, json_mode=json_out)
    else:
        _file_require_ssh(remote)
        try:
            payload = transfer.ssh_file_stat(remote, path)
        except transfer.TransferError as e:
            err(f"error: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
    if json_out:
        print_json(payload)
        return
    if not payload.get("exists", True):
        print(f"missing\t{path}")
        return
    print(f"{payload.get('type')}\t{payload.get('size')}\t{path}")


@file_app.command("read", help="Read a remote file to stdout (binary-safe).")
def cmd_file_read(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    path: Annotated[str, typer.Option("--path", help="File path.")],
    b64: Annotated[bool, typer.Option("--b64", help="Output base64 instead of raw bytes.")] = False,
):
    client, info = _bridge_remote(remote)
    if client is not None and info is not None:
        try:
            resp = client.file_read(remote=remote, path=path)
        except bridge.BridgeError as e:
            _exit_bridge_error(e)
        data = base64.b64decode(resp.get("content_b64") or "")
    else:
        _file_require_ssh(remote)
        try:
            data = transfer.ssh_file_read(remote, path)
        except transfer.TransferError as e:
            err(f"error: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
    if b64:
        sys.stdout.write(base64.b64encode(data).decode("ascii") + "\n")
    else:
        sys.stdout.buffer.write(data)


@file_app.command("write", help="Write content to a remote file (binary-safe).")
def cmd_file_write(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    path: Annotated[str, typer.Option("--path", help="File path.")],
    content: Annotated[Optional[str], typer.Option("--content", help="Text content (utf-8).")] = None,
    stdin: Annotated[bool, typer.Option("--stdin", help="Read bytes from stdin.")] = False,
    b64: Annotated[Optional[str], typer.Option("--b64", help="Base64-encoded content.")] = None,
    mode: Annotated[str, typer.Option("--mode", help="File mode, e.g. 644 or 0o755.")] = "644",
):
    provided = sum(1 for x in (content, b64) if x is not None) + (1 if stdin else 0)
    if provided != 1:
        err("error: exactly one of --content / --stdin / --b64 is required")
        raise typer.Exit(EXIT_USER_ERROR)
    if stdin:
        data = sys.stdin.buffer.read()
    elif b64 is not None:
        try:
            data = base64.b64decode(b64, validate=True)
        except Exception as e:
            err(f"error: invalid base64 payload: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
    else:
        data = (content or "").encode("utf-8")
    mode_int = _parse_file_mode(mode)

    client, info = _bridge_remote(remote)
    if client is not None and info is not None:
        try:
            client.file_write(
                remote=remote,
                path=path,
                content_b64=base64.b64encode(data).decode("ascii"),
                mode=mode_int,
                max_size=transfer.DEFAULT_BRIDGE_MAX_FILE_SIZE,
            )
        except bridge.BridgeError as e:
            _exit_bridge_error(e)
    else:
        _file_require_ssh(remote)
        try:
            transfer.ssh_file_write(remote, path, data, mode=mode_int)
        except transfer.TransferError as e:
            err(f"error: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
    print(f"wrote {len(data)} byte(s) to {remote}:{path}")


# ---------- read / apply-patch (top-level, absolute-path only) ----------


@app.command(
    "read",
    help=(
        "Read a remote file and print a metadata header followed by numbered content.\n"
        "Header: path, detected encoding (utf-8/utf-8-sig/utf-16/utf-32/latin-1/binary),\n"
        "size in bytes with line range, and sha256. Content lines are prefixed 'N→'\n"
        "(cat -n style) unless --no-lineno. Use --offset/--limit to page through long\n"
        "files instead of dumping everything. --raw emits the original bytes only\n"
        "(redirect to a file for a faithful copy); --json emits one machine-readable\n"
        "object. Binary files report 'binary' and show a byte count instead of content\n"
        "(--json adds content_b64). The path must be ABSOLUTE; relative paths are\n"
        "rejected to avoid hitting the wrong tree."
    ),
)
def cmd_read(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    path: Annotated[str, typer.Option("--path", help="Absolute path of the file to read.")],
    offset: Annotated[int, typer.Option("--offset", help="First line to show (1-based). Defaults to 1.")] = 1,
    limit: Annotated[int, typer.Option("--limit", help="Max lines to show. Defaults to 2000.")] = 2000,
    json_out: Annotated[bool, typer.Option("--json", help="Output a single JSON object.")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="Raw bytes only, no header or line numbers.")] = False,
    no_lineno: Annotated[bool, typer.Option("--no-lineno", help="Content without line numbers.")] = False,
):
    path = _require_absolute(path)
    if offset < 1:
        err("error: --offset must be >= 1")
        raise typer.Exit(EXIT_USER_ERROR)
    if limit < 1:
        err("error: --limit must be >= 1")
        raise typer.Exit(EXIT_USER_ERROR)

    data = _read_remote_bytes(remote, path)
    encoding = _detect_encoding(data)
    size = len(data)
    digest = hashlib.sha256(data).hexdigest()

    if raw:
        sys.stdout.buffer.write(data)
        return

    if encoding == "binary":
        content = f"<binary {size} bytes>"
        total_lines = 0
        shown = (0, 0)
        truncated = False
    else:
        text = data.decode(encoding, errors="replace")
        lines = text.splitlines()
        total_lines = len(lines)
        start = min(offset, total_lines + 1)
        picked = lines[start - 1 : start - 1 + limit]
        shown = (start, start + len(picked) - 1)
        truncated = len(picked) < total_lines
        if no_lineno:
            content = "\n".join(picked)
        else:
            content = "\n".join(f"{i}→{line}" for i, line in zip(range(start, start + len(picked)), picked))

    if json_out:
        payload: dict[str, object] = {
            "path": path,
            "encoding": encoding,
            "size": size,
            "sha256": digest,
            "total_lines": total_lines,
            "offset": shown[0],
            "limit": shown[1] - shown[0] + 1 if shown[1] >= shown[0] else 0,
            "truncated": truncated,
            "content": content,
        }
        if encoding == "binary":
            payload["content_b64"] = base64.b64encode(data).decode("ascii")
        print_json(payload)
        return

    print(f"path:     {path}")
    print(f"encoding: {encoding}")
    if total_lines:
        print(f"size:     {size} bytes (lines 1-{total_lines}; showing {shown[0]}-{shown[1]})")
    else:
        print(f"size:     {size} bytes")
    print(f"sha256:   {digest}")
    print("─" * 44)
    if content:
        print(content)


@app.command(
    "apply-patch",
    help=(
        "Apply a patch to create, edit, or delete files.\n"
        "Patch shape (read from stdin, or a file via --file):\n"
        "  *** Begin Patch\n"
        "  *** Update File: /abs/path/file.py\n"
        "  @@ context line\n"
        "  -old line\n"
        "  +new line\n"
        "  *** End Patch\n"
        "Supported ops: *** Add File: / *** Update File: / *** Delete File:.\n"
        "Paths must be ABSOLUTE — a relative path is rejected with 'patch path must be absolute'.\n"
        "Inside each '@@ ...' hunk: a single leading space marks a context line (anchor — must\n"
        "match the file byte-for-byte, including blank lines and indentation); '-' lines are\n"
        "removed; '+' lines are inserted. A context mismatch raises 'patch context not found'.\n"
        "Rules: (1) one edit site per @@ hunk — never bundle distant locations into one giant\n"
        "hunk; (2) copy context lines verbatim from the file; (3) prefer a few small hunks.\n"
        "Targets: omit --remote for local files (atomic tmp+rename on update); --remote picks\n"
        "a bridge or SSH remote. --dry-run validates context without writing ('would ...').\n"
        "Add File refuses to overwrite an existing file unless --force is given."
    ),
)
def cmd_apply_patch(
    patch_file: Annotated[
        Optional[str],
        typer.Option("--file", help="Read the patch from this file instead of stdin."),
    ] = None,
    remote: Annotated[
        Optional[str],
        typer.Option("--remote", help="Bridge or SSH remote to patch on. Omit for local files."),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Validate context matches without writing.")] = False,
    force: Annotated[bool, typer.Option("--force", help="Let Add File overwrite an existing file.")] = False,
):
    from . import mcp_server

    if patch_file is not None:
        try:
            patch_text = Path(patch_file).read_text(encoding="utf-8")
        except OSError as e:
            err(f"error: cannot read patch file: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
    else:
        patch_text = sys.stdin.read()
    if not patch_text.strip():
        err("error: empty patch input")
        raise typer.Exit(EXIT_USER_ERROR)

    try:
        ops = mcp_server._parse_codex_patch(patch_text)
    except Exception as e:
        err(f"error: cannot parse patch: {e}")
        raise typer.Exit(EXIT_USER_ERROR)
    if not ops:
        err("error: patch contains no file operations")
        raise typer.Exit(EXIT_USER_ERROR)

    bad = [op["path"] for op in ops if not (
        op["path"].startswith(("/", "\\"))
        or (len(op["path"]) >= 3 and op["path"][1] == ":" and op["path"][2] in ("/", "\\"))
    )]
    if bad:
        err("error: patch paths must be absolute:")
        for p in bad:
            err(f"  - {p}")
        raise typer.Exit(EXIT_USER_ERROR)

    if remote is not None:
        client, info = _bridge_remote(remote)
        if client is not None and info is not None:
            if dry_run:
                results = []
                for op in ops:
                    p = op["path"]
                    if op["kind"] == "update":
                        orig = _read_remote_bytes(remote, p).decode("utf-8", "replace")
                        mcp_server._apply_hunks(orig, op["hunks"])  # raises on context mismatch
                        results.append(f"would update: {p}")
                    else:
                        results.append(f"would {op['kind']}: {p}")
                for line in results:
                    print(line)
                return
            applied = mcp_server._remote_apply_patch(remote, "", {"input": patch_text})
            for line in applied["applied"]:
                print(line)
            return
        # SSH remote: read → apply → write back, one round-trip per updated file.
        _file_require_ssh(remote)
        try:
            for op in ops:
                p = op["path"]
                if op["kind"] == "add":
                    data = op["content"].encode("utf-8")
                    if not force:
                        # Refuse to clobber: stat first via a read attempt.
                        try:
                            transfer.ssh_file_read(remote, p)
                            err(f"error: {p} already exists (use --force to overwrite)")
                            raise typer.Exit(EXIT_USER_ERROR)
                        except transfer.TransferError:
                            pass
                    if not dry_run:
                        transfer.ssh_file_write(remote, p, data)
                    print(f"{'would ' if dry_run else ''}added: {p}")
                elif op["kind"] == "delete":
                    raise typer.Exit(_ssh_delete_not_supported())
                else:
                    orig_bytes = transfer.ssh_file_read(remote, p)
                    orig = orig_bytes.decode("utf-8", "replace")
                    new = mcp_server._apply_hunks(orig, op["hunks"])
                    if not dry_run:
                        transfer.ssh_file_write(remote, p, new.encode("utf-8"))
                    print(f"{'would ' if dry_run else ''}updated: {p}")
        except transfer.TransferError as e:
            err(f"error: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
        return

    # Local mode: same engine, local IO, atomic replace on update.
    import tempfile

    for op in ops:
        p = op["path"]
        target = Path(p)
        if op["kind"] == "add":
            if target.exists() and not force:
                err(f"error: {p} already exists (use --force to overwrite)")
                raise typer.Exit(EXIT_USER_ERROR)
            if dry_run:
                print(f"would add: {p}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(op["content"], encoding="utf-8")
            print(f"added: {p}")
        elif op["kind"] == "delete":
            if dry_run:
                print(f"would delete: {p}")
                continue
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            print(f"deleted: {p}")
        else:
            orig = target.read_text(encoding="utf-8", errors="replace")
            new = mcp_server._apply_hunks(orig, op["hunks"])
            if dry_run:
                print(f"would update: {p}")
                continue
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".aimux-patch-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(new)
                os.replace(tmp, target)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
            print(f"updated: {p}")


def _ssh_delete_not_supported() -> int:
    err("error: Delete File over SSH is not supported; use 'aimux file delete' instead")
    return EXIT_USER_ERROR


@file_app.command("mkdir", help="Create a remote directory (with parents).")
def cmd_file_mkdir(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    path: Annotated[str, typer.Option("--path", help="Directory path.")],
):
    client, info = _bridge_remote(remote)
    if client is not None and info is not None:
        try:
            client.file_mkdir(remote=remote, path=path)
        except bridge.BridgeError as e:
            _exit_bridge_error(e)
    else:
        _file_require_ssh(remote)
        try:
            transfer.ssh_file_mkdir(remote, path)
        except transfer.TransferError as e:
            err(f"error: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
    print(f"created directory {remote}:{path}")


@file_app.command("delete", help="Delete a remote file or directory.")
def cmd_file_delete(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    path: Annotated[str, typer.Option("--path", help="Path to delete.")],
    recursive: Annotated[bool, typer.Option("--recursive", help="Required to delete a directory.")] = False,
):
    client, info = _bridge_remote(remote)
    if client is not None and info is not None:
        try:
            client.file_delete(remote=remote, path=path, recursive=recursive)
        except bridge.BridgeError as e:
            _exit_bridge_error(e)
    else:
        _file_require_ssh(remote)
        try:
            transfer.ssh_file_delete(remote, path, recursive=recursive)
        except transfer.TransferError as e:
            err(f"error: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
    print(f"deleted {remote}:{path}")


@file_app.command("rename", help="Rename or move a remote path.")
def cmd_file_rename(
    remote: Annotated[str, typer.Argument(help="SSH host or bridge remote name.")],
    src: Annotated[str, typer.Option("--src", help="Source path.")],
    dst: Annotated[str, typer.Option("--dst", help="Destination path.")],
):
    client, info = _bridge_remote(remote)
    if client is not None and info is not None:
        try:
            client.file_rename(remote=remote, src=src, dst=dst)
        except bridge.BridgeError as e:
            _exit_bridge_error(e)
    else:
        _file_require_ssh(remote)
        try:
            transfer.ssh_file_rename(remote, src, dst)
        except transfer.TransferError as e:
            err(f"error: {e}")
            raise typer.Exit(EXIT_USER_ERROR)
    print(f"renamed {remote}:{src} -> {remote}:{dst}")


# ---------- mcp serve ----------

@mcp_app.command("serve", help="Run a stdio MCP server for a bridge remote (codex-facing tools).")
def cmd_mcp_serve(
    remote: Annotated[str, typer.Option("--remote", help="Bridge remote name (aimux_id) to expose.")],
    profile: Annotated[
        Optional[str],
        typer.Option("--profile", help="Override the bridge session profile."),
    ] = None,
):
    from . import mcp_server  # lazy import: keep `mcp` off the common command path
    try:
        mcp_server.run_stdio(remote=remote, profile=profile)
    except bridge.BridgeError as e:
        _exit_bridge_error(e)
    except KeyboardInterrupt:
        raise typer.Exit(EXIT_OK)
