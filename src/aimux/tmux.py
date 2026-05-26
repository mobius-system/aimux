"""Thin subprocess wrapper around `tmux -L aimux ...`."""
from __future__ import annotations

import shlex
import subprocess
from typing import Iterable

from .constants import SOCKET_NAME, aimux_conf_path


class TmuxError(RuntimeError):
    def __init__(self, returncode: int, stderr: str, cmd: list[str]):
        super().__init__(f"tmux failed (exit {returncode}): {stderr.strip()}")
        self.returncode = returncode
        self.stderr = stderr
        self.cmd = cmd


def _base() -> list[str]:
    # -f loads aimux.conf when the server is first started; harmless on later calls.
    return ["tmux", "-L", SOCKET_NAME, "-f", str(aimux_conf_path())]


def run(args: Iterable[str], *, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a tmux subcommand. Returns CompletedProcess with text=True."""
    cmd = _base() + list(args)
    r = subprocess.run(
        cmd,
        capture_output=capture,
        text=True,
    )
    if check and r.returncode != 0:
        raise TmuxError(r.returncode, r.stderr or "", cmd)
    return r


def server_running() -> bool:
    """Probe whether the aimux tmux server is up."""
    r = subprocess.run(
        ["tmux", "-L", SOCKET_NAME, "ls"],
        capture_output=True, text=True,
    )
    # tmux returns 1 with "no server running on /tmp/tmux-…/aimux"
    return r.returncode == 0


def list_sessions_raw(fmt: str) -> list[str]:
    """Return raw lines from `tmux ls -F '<fmt>'`. Empty if no server / no sessions."""
    if not server_running():
        return []
    r = subprocess.run(
        _base() + ["ls", "-F", fmt],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        # `ls` with no sessions exits 1 with "no server running" — treat as empty.
        return []
    return [ln for ln in r.stdout.splitlines() if ln]


def has_session(name: str) -> bool:
    if not server_running():
        return False
    r = subprocess.run(
        _base() + ["has-session", "-t", name],
        capture_output=True, text=True,
    )
    return r.returncode == 0


def get_option(name: str, key: str) -> str | None:
    """Read a session-scoped user option. Returns None if unset."""
    r = subprocess.run(
        _base() + ["show-options", "-v", "-t", name, key],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    s = r.stdout.strip()
    return s or None


def set_option(name: str, key: str, value: str) -> None:
    run(["set-option", "-t", name, key, value])


def new_session(name: str, *, cwd: str | None, command: str | None) -> None:
    args = ["new-session", "-d", "-s", name]
    if cwd:
        args += ["-c", cwd]
    if command:
        args += [command]
    run(args)


def kill_session(name: str) -> None:
    run(["kill-session", "-t", name])


def attach(name: str, *, read_only: bool = False) -> None:
    args = ["attach-session", "-t", name]
    if read_only:
        args.append("-r")
    # attach is interactive: do not capture output, do not check return.
    subprocess.run(_base() + args)


def send_keys(name: str, keys: list[str], *, flags: list[str]) -> None:
    args = ["send-keys"] + flags + ["-t", name] + keys
    run(args)


def capture_pane(name: str, *, lines: int, ansi: bool) -> str:
    # `-S -N` starts at history line -N and ends at the visible cursor; we
    # then trim the result to the actual last N non-trailing-blank lines so
    # `--lines=N` means exactly N.
    args = ["capture-pane", "-p", "-J", "-t", name, "-S", f"-{lines}"]
    if ansi:
        args.insert(args.index("-p") + 1, "-e")
    r = run(args)
    text = r.stdout
    parts = text.split("\n")
    # Drop trailing pure-blank lines (capture often pads visible area with empties).
    while parts and parts[-1].strip() == "":
        parts.pop()
    parts = parts[-lines:]
    return "\n".join(parts) + "\n" if parts else ""


def list_panes_current_command(name: str) -> list[str] | None:
    """Return a list of pane_current_command entries, or None if session is gone."""
    r = subprocess.run(
        _base() + ["list-panes", "-F", "#{pane_current_command}", "-t", name],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0:
        return None
    return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]


def pipe_pane(name: str, shell_command: str) -> None:
    """Set up output piping for the pane. Replaces any existing pipe."""
    run(["pipe-pane", "-t", name, shell_command])


def quote(s: str) -> str:
    return shlex.quote(s)
