"""Reverse bridge client and terminal profile adapters."""
from __future__ import annotations

import base64
from collections import deque
import hashlib
import http.client
import json
import locale
import os
import platform
import queue
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from . import __version__
from . import port_tunnel
from .bridge import BridgeError, validate_identifier, version_mismatch_message
from .transfer import (
    DEFAULT_BRIDGE_MAX_ARCHIVE_SIZE,
    apply_sync_archive_b64,
    build_sync_archive_b64,
    validate_bridge_path,
)


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_STREAM_EOF = object()
BRIDGE_CLIENT_HEARTBEAT_INTERVAL = 180.0
WATCHDOG_INTERVAL = 5.0
WATCHDOG_TIMEOUT = 30.0
RUNTIME_WRITE_LOCK = threading.Lock()
# Streaming file uploads are read/sent in 1 MiB chunks so the reverse client
# never holds a whole file in memory while it uploads over its own outbound
# data connection.
STREAM_CHUNK_SIZE = 1024 * 1024

# Backoff schedule used when /client/register returns 409 Conflict (a stale
# server-side record for the same identifier) or the broker is temporarily
# unreachable. Lets the client ride out a broker restart or slow disconnect
# detection instead of exiting and relying on an external supervisor.
REGISTER_RETRY_INTERVALS: tuple[float, ...] = (2.0, 4.0, 8.0, 16.0, 30.0)


def _payload_count(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    return len(value) if isinstance(value, list) else 0


def _bridge_command_summary(message_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    if message_type == "session.create":
        return {
            "session": payload.get("name"),
            "profile": payload.get("profile"),
            "cmd": payload.get("cmd"),
            "cwd": payload.get("cwd"),
        }
    if message_type == "session.send_keys":
        keys = payload.get("keys") if isinstance(payload.get("keys"), list) else []
        flags = payload.get("flags") if isinstance(payload.get("flags"), list) else []
        return {"session": payload.get("session"), "keys": keys, "flags": flags}
    if message_type == "session.capture":
        return {"session": payload.get("session"), "lines": payload.get("lines"), "ansi": bool(payload.get("ansi"))}
    if message_type == "session.kill":
        return {"session": payload.get("session")}
    if message_type == "session.list":
        return {}
    if message_type == "file.write":
        content_b64 = str(payload.get("content_b64") or "")
        return {
            "path": payload.get("path"),
            "content_b64_len": len(content_b64),
            "max_size": payload.get("max_size"),
            "mode": payload.get("mode"),
        }
    if message_type == "file.archive":
        return {
            "path": payload.get("path"),
            "rel_paths": _payload_count(payload, "rel_paths"),
            "dirs": _payload_count(payload, "dirs"),
            "max_size": payload.get("max_size"),
        }
    if message_type == "file.apply_archive":
        archive_b64 = str(payload.get("archive_b64") or "")
        return {
            "path": payload.get("path"),
            "archive_b64_len": len(archive_b64),
            "delete_paths": _payload_count(payload, "delete_paths"),
            "max_size": payload.get("max_size"),
        }
    if message_type.startswith("file."):
        return {
            "path": payload.get("path"),
            "recursive": payload.get("recursive"),
            "max_size": payload.get("max_size"),
        }
    return {"payload_keys": sorted(payload)}


def _is_logged_bridge_command(message_type: str) -> bool:
    return message_type.startswith("file.") or message_type.startswith("session.")


@dataclass
class CaptureResult:
    content: str
    lines: int
    ansi: bool = False
    source: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "lines": self.lines, "ansi": self.ansi, "source": self.source}


@dataclass
class ClientSession:
    name: str
    profile: str
    command: str
    cwd: str | None
    created_at: str
    process: subprocess.Popen[Any] | None = None
    pid: int | None = None
    adapter: "TerminalAdapter | None" = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "profile": self.profile,
            "pid": self.pid,
            "cmd": self.command,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "status": "running" if self.is_running() else "exited",
        }

    def is_running(self) -> bool:
        if self.process is not None:
            return self.process.poll() is None
        return True


class TerminalAdapter(Protocol):
    name: str
    visible: bool
    capture_source: str
    supports_ansi: bool

    def profile_info(self) -> dict[str, Any]:
        ...

    def create(self, *, name: str, cmd: str | None, cwd: str | None, created_at: str) -> ClientSession:
        ...

    def send_keys(self, session: ClientSession, keys: list[str], flags: list[str]) -> None:
        ...

    def capture(self, session: ClientSession, lines: int, ansi: bool) -> CaptureResult:
        ...

    def kill(self, session: ClientSession) -> None:
        ...


def _parse_repeat(flags: list[str]) -> tuple[int, bool]:
    literal = False
    repeat = 1
    i = 0
    while i < len(flags):
        flag = flags[i]
        if flag == "-l":
            literal = True
            i += 1
            continue
        if flag == "-N":
            if i + 1 >= len(flags):
                raise BridgeError("-N requires a count")
            try:
                repeat = int(flags[i + 1])
            except ValueError:
                raise BridgeError("-N count must be an integer")
            if repeat <= 0:
                raise BridgeError("-N count must be positive")
            i += 2
            continue
        raise BridgeError(f"bridge sessions do not support tmux flag {flag!r}")
    return repeat, literal


def _keys_to_text(keys: list[str], flags: list[str]) -> str:
    repeat, literal = _parse_repeat(flags)
    out: list[str] = []
    specials = {
        "Enter": "\r",
        "C-m": "\r",
        "Tab": "\t",
        "Space": " ",
        "Backspace": "\x7f",
        "BSpace": "\x7f",
        "Escape": "\x1b",
        "Esc": "\x1b",
        "Up": "\x1b[A",
        "Down": "\x1b[B",
        "Right": "\x1b[C",
        "Left": "\x1b[D",
        "Home": "\x1b[H",
        "End": "\x1b[F",
        "Delete": "\x1b[3~",
        "DC": "\x1b[3~",
        "C-c": "\x03",
        "C-d": "\x04",
        "C-l": "\x0c",
    }
    for _ in range(repeat):
        for key in keys:
            out.append(key if literal else specials.get(key, key))
    return "".join(out)


def _find_posix_shell() -> str:
    """Return the first available POSIX shell on this system.

    Some minimal container images (DLC training pods, distroless, etc.) ship
    only ``/bin/bash`` or ``/bin/dash`` without a ``/bin/sh`` symlink. Probe
    the common candidates so command sessions always find a working shell.
    """
    for sh in ("/bin/bash", "/bin/dash", "/bin/sh"):
        if os.path.exists(sh):
            return sh
    return "/bin/sh"  # fallback — will give a clear FileNotFoundError


class PosixPtyAdapter:
    visible = False
    capture_source = "pty-ring-buffer"
    supports_ansi = True

    def __init__(self, name: str, command: str | None = None):
        self.name = name
        self.command = command
        self._buffers: dict[str, list[str]] = {}
        self._fds: dict[str, int] = {}
        self._lock = threading.RLock()

    def profile_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "visible": self.visible,
            "capture": self.capture_source,
            "ansi": self.supports_ansi,
            "available": True,
        }

    def create(self, *, name: str, cmd: str | None, cwd: str | None, created_at: str) -> ClientSession:
        import pty

        requested_command = cmd or self.command
        default_shell = os.environ.get("SHELL") or _find_posix_shell()
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")

        if requested_command:
            tokens = shlex.split(requested_command)
            executable_name = Path(tokens[0]).name.lower() if tokens else ""
            shell_names = {"bash", "dash", "fish", "ksh", "sh", "zsh"}
            if executable_name in shell_names and all(part.startswith("-") for part in tokens[1:]):
                # MCP's explicit ``shell`` option is a shell specification, not
                # a one-shot command. Launch it directly and force interactive
                # mode so it stays alive for send_keys/capture.
                argv = tokens if "-i" in tokens else [*tokens, "-i"]
            else:
                # Preserve the historical --cmd shell syntax (pipes,
                # redirections, etc.) while avoiding shell=True's nested shell.
                argv = [_find_posix_shell(), "-lc", requested_command]
        else:
            # A direct interactive shell is critical here.  The old path used
            # shell=True plus a preexec_fn that called setsid/TIOCSCTTY.  Some
            # bundled/conda Python runtimes crash that fork child with SIGSEGV
            # before exec, so create appeared successful but the first
            # send_keys immediately reported session_gone.
            argv = [default_shell, "-i"]

        process = subprocess.Popen(
            argv,
            shell=False,
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        session = ClientSession(
            name=name,
            profile=self.name,
            command=requested_command or default_shell,
            cwd=cwd,
            created_at=created_at,
            process=process,
            pid=process.pid,
            adapter=self,
        )
        with self._lock:
            self._buffers[name] = []
            self._fds[name] = master_fd
        thread = threading.Thread(target=self._reader, args=(name, master_fd), daemon=True)
        thread.start()
        return session

    def _reader(self, name: str, fd: int) -> None:
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", errors="replace")
            with self._lock:
                buf = self._buffers.get(name)
                if buf is None:
                    break
                buf.append(text)
                total = sum(len(part) for part in buf)
                while total > 1_000_000 and len(buf) > 1:
                    total -= len(buf.pop(0))

    def send_keys(self, session: ClientSession, keys: list[str], flags: list[str]) -> None:
        text = _keys_to_text(keys, flags)
        with self._lock:
            fd = self._fds.get(session.name)
        if fd is None:
            raise BridgeError(f"session {session.name!r} is gone", kind="session_gone")
        os.write(fd, text.encode("utf-8"))

    def capture(self, session: ClientSession, lines: int, ansi: bool) -> CaptureResult:
        with self._lock:
            text = "".join(self._buffers.get(session.name, []))
        if not ansi:
            text = _ANSI_RE.sub("", text)
        rows = text.splitlines()
        rows = rows[-lines:]
        content = "\n".join(rows)
        if content:
            content += "\n"
        return CaptureResult(content=content, lines=lines, ansi=ansi, source=self.capture_source)

    def kill(self, session: ClientSession) -> None:
        if session.process and session.process.poll() is None:
            try:
                os.killpg(session.process.pid, signal.SIGTERM)
            except Exception:
                session.process.terminate()
            try:
                session.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                session.process.kill()
        with self._lock:
            fd = self._fds.pop(session.name, None)
            self._buffers.pop(session.name, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class WindowsConsoleAdapter:
    visible = True
    capture_source = "screen-buffer"
    supports_ansi = False

    def __init__(self, name: str, command: str, silent: bool = False):
        self.name = name
        self.command = command
        self.silent = silent

    def profile_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "visible": self.visible,
            "capture": self.capture_source,
            "ansi": self.supports_ansi,
            "available": True,
        }

    def create(self, *, name: str, cmd: str | None, cwd: str | None, created_at: str) -> ClientSession:
        if sys.platform != "win32":
            raise BridgeError(f"profile {self.name!r} is only available on Windows")
        command = cmd or self.command
        creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        startupinfo = None
        if self.silent:
            # CREATE_NEW_CONSOLE still allocates a console buffer (attach/capture
            # keep working), but STARTF_USESHOWWINDOW + SW_HIDE keeps its window
            # off the desktop — shells run in the background.
            si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
            si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            si.wShowWindow = 0  # SW_HIDE
            startupinfo = si
        process = subprocess.Popen(command, cwd=cwd, creationflags=creationflags, startupinfo=startupinfo)
        session = ClientSession(
            name=name,
            profile=self.name,
            command=command,
            cwd=cwd,
            created_at=created_at,
            process=process,
            pid=process.pid,
            adapter=self,
        )
        try:
            from . import bridge_windows

            bridge_windows.configure_buffer(process.pid, height=9999)
        except Exception:
            pass
        return session

    def send_keys(self, session: ClientSession, keys: list[str], flags: list[str]) -> None:
        from . import bridge_windows

        bridge_windows.send_keys(int(session.pid or 0), keys, flags)

    def capture(self, session: ClientSession, lines: int, ansi: bool) -> CaptureResult:
        from . import bridge_windows

        content = bridge_windows.capture(int(session.pid or 0), lines)
        return CaptureResult(content=content, lines=lines, ansi=False, source=self.capture_source)

    def kill(self, session: ClientSession) -> None:
        if session.pid:
            subprocess.run(["taskkill", "/PID", str(session.pid), "/T", "/F"], capture_output=True, text=True)


class WindowsPipeAdapter:
    """A completely hidden Windows shell backed by ordinary pipes.

    ``CREATE_NEW_CONSOLE`` (the implementation used by ``--silent-shell``)
    necessarily creates a console window before ``SW_HIDE`` can hide it.  That
    race is the source of the one-frame flash seen by Electron/TUI users.  The
    v2 profile uses ``CREATE_NO_WINDOW`` and redirected standard handles, so no
    console is ever allocated.  A small reader thread provides the same
    send/capture contract as the console-buffer adapter without relying on
    ``AttachConsole``.
    """

    visible = False
    capture_source = "pipe-ring-buffer"
    supports_ansi = True
    _MAX_CAPTURE_CHARS = 512 * 1024

    def __init__(self, name: str, command: str):
        self.name = name
        self.command = command

    def _stdin_encoding(self) -> str:
        """Return the byte encoding expected by redirected Windows shells.

        Bridge messages carry Unicode text, but cmd.exe and Windows PowerShell
        5.1 decode redirected stdin using the Windows console/OEM code page.
        Writing UTF-8 unconditionally therefore turns Chinese text into
        mojibake on CP936 systems. CREATE_NO_WINDOW may leave this process
        without a console (GetConsoleCP returns 0), so retain Windows' OEM and
        ANSI code pages as deterministic fallbacks.
        """
        if self.name in {"mingw64", "pwsh"}:
            # Git Bash and modern PowerShell use UTF-8 for redirected input.
            return "utf-8"
        if sys.platform == "win32":
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32
                for getter_name in ("GetConsoleCP", "GetOEMCP", "GetACP"):
                    getter = getattr(kernel32, getter_name, None)
                    if getter is None:
                        continue
                    code_page = int(getter())
                    if code_page > 0:
                        return "utf-8" if code_page == 65001 else f"cp{code_page}"
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        return locale.getpreferredencoding(False) or "utf-8"

    def profile_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "visible": self.visible,
            "capture": self.capture_source,
            "ansi": self.supports_ansi,
            "available": True,
        }

    @classmethod
    def _append_output(cls, session: ClientSession, chunk: bytes) -> None:
        if not chunk:
            return
        lock = session.metadata["output_lock"]
        with lock:
            output: deque[str] = session.metadata["output"]
            try:
                decoded = chunk.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                # Native cmd/Windows PowerShell output may use the same OEM
                # code page as stdin, while child programs can still emit
                # UTF-8. Prefer UTF-8 per chunk and fall back only when it is
                # invalid, preserving both common sources in one session.
                fallback = session.metadata.get("output_fallback_encoding") or "utf-8"
                decoded = chunk.decode(fallback, errors="replace")
            output.append(decoded)
            # Keep a bounded tail without joining on every read.  A command
            # producing a large log must not retain the whole process lifetime.
            total = session.metadata["output_chars"] + len(output[-1])
            session.metadata["output_chars"] = total
            while total > cls._MAX_CAPTURE_CHARS and output:
                total -= len(output.popleft())
            session.metadata["output_chars"] = total

    @classmethod
    def _reader(cls, session: ClientSession) -> None:
        stream = session.process.stdout if session.process is not None else None
        if stream is None:
            return
        try:
            while True:
                # BufferedReader.read1 returns as soon as bytes are available,
                # unlike readline which would hide an interactive shell prompt.
                read1 = stream.read1 if hasattr(stream, "read1") else stream.read
                chunk = read1(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                cls._append_output(session, chunk)
        except (OSError, ValueError):
            # The process may close its pipe while the bridge is shutting down.
            pass

    def create(self, *, name: str, cmd: str | None, cwd: str | None, created_at: str) -> ClientSession:
        if sys.platform != "win32":
            raise BridgeError(f"profile {self.name!r} is only available on Windows")
        command = cmd or self.command
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        process = subprocess.Popen(
            command,
            cwd=cwd,
            creationflags=no_window,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
        )
        stdin_encoding = self._stdin_encoding()
        session = ClientSession(
            name=name,
            profile=self.name,
            command=command,
            cwd=cwd,
            created_at=created_at,
            process=process,
            pid=process.pid,
            adapter=self,
            metadata={
                "output": deque(),
                "output_chars": 0,
                "output_lock": threading.Lock(),
                "stdin_encoding": stdin_encoding,
                "output_fallback_encoding": stdin_encoding,
            },
        )
        thread = threading.Thread(target=self._reader, args=(session,), name=f"aimux-{name}-output", daemon=True)
        session.metadata["output_thread"] = thread
        thread.start()
        return session

    def send_keys(self, session: ClientSession, keys: list[str], flags: list[str]) -> None:
        stream = session.process.stdin if session.process is not None else None
        if stream is None:
            raise BridgeError(f"session {session.name!r} has no writable stdin", kind="session_gone")
        text = _keys_to_text(keys, flags)
        if not text:
            return
        # cmd.exe and PowerShell read CRLF from redirected stdin.  Preserve
        # literal mode and control bytes while normalising Enter generated by
        # the bridge key protocol.
        text = re.sub(r"\r\n|\r|\n", "\r\n", text)
        try:
            encoding = session.metadata.get("stdin_encoding") or self._stdin_encoding()
            try:
                payload = text.encode(encoding)
            except (LookupError, UnicodeEncodeError) as exc:
                raise BridgeError(
                    f"session {session.name!r} input contains characters that cannot be encoded as {encoding}",
                    kind="encoding_error",
                ) from exc
            stream.write(payload)
            stream.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise BridgeError(f"session {session.name!r} stdin is closed", kind="session_gone") from exc

    def capture(self, session: ClientSession, lines: int, ansi: bool) -> CaptureResult:
        if lines <= 0:
            raise BridgeError("--lines must be a positive integer")
        with session.metadata["output_lock"]:
            content = "".join(session.metadata["output"])
        if not ansi:
            content = _ANSI_RE.sub("", content)
        rows = content.splitlines()
        content = "\n".join(rows[-lines:])
        if content:
            content += "\n"
        return CaptureResult(content=content, lines=lines, ansi=ansi, source=self.capture_source)

    def kill(self, session: ClientSession) -> None:
        process = session.process
        if process is None or process.poll() is not None:
            return
        # Terminating only cmd.exe can leave a child shell behind.  taskkill
        # handles the process tree, and CREATE_NO_WINDOW prevents taskkill from
        # becoming a second source of console flashes.
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                creationflags=no_window,
                check=False,
            )
        except (OSError, ValueError):
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()


class MacTerminalAdapter:
    name = "mac-terminal"
    visible = True
    capture_source = "terminal-contents"
    supports_ansi = False

    def profile_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "visible": self.visible,
            "capture": self.capture_source,
            "ansi": self.supports_ansi,
            "available": sys.platform == "darwin",
            "requires_permission": "Automation",
        }

    def create(self, *, name: str, cmd: str | None, cwd: str | None, created_at: str) -> ClientSession:
        command = cmd or os.environ.get("SHELL") or "/bin/zsh"
        shell_command = command
        if cwd:
            shell_command = f"cd {shlex.quote(cwd)}; exec {command}"
        script = (
            'tell application "Terminal"\n'
            "  activate\n"
            f"  do script {json.dumps(shell_command)}\n"
            "  delay 0.2\n"
            "  return id of front window\n"
            "end tell\n"
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True)
        window_id = result.stdout.strip()
        return ClientSession(
            name=name,
            profile=self.name,
            command=command,
            cwd=cwd,
            created_at=created_at,
            pid=None,
            adapter=self,
            metadata={"window_id": window_id},
        )

    def send_keys(self, session: ClientSession, keys: list[str], flags: list[str]) -> None:
        text = _keys_to_text(keys, flags).replace("\r", "\n")
        window_id = session.metadata.get("window_id")
        script = (
            'tell application "Terminal"\n'
            f"  do script {json.dumps(text)} in window id {window_id}\n"
            "end tell\n"
        )
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True)

    def capture(self, session: ClientSession, lines: int, ansi: bool) -> CaptureResult:
        window_id = session.metadata.get("window_id")
        script = (
            'tell application "Terminal"\n'
            f"  return contents of selected tab of window id {window_id}\n"
            "end tell\n"
        )
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True)
        rows = result.stdout.splitlines()[-lines:]
        content = "\n".join(row.rstrip() for row in rows)
        if content:
            content += "\n"
        return CaptureResult(content=content, lines=lines, ansi=False, source=self.capture_source)

    def kill(self, session: ClientSession) -> None:
        window_id = session.metadata.get("window_id")
        script = f'tell application "Terminal" to close window id {window_id}'
        subprocess.run(["osascript", "-e", script], capture_output=True, text=True)


def find_git_bash() -> str | None:
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        str(Path.home() / "scoop" / "apps" / "git" / "current" / "bin" / "bash.exe"),
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    return shutil.which("bash.exe")


def build_adapters(silent_shell: bool = False, silent_shell_v2: bool = False) -> tuple[dict[str, TerminalAdapter], str]:
    adapters: dict[str, TerminalAdapter] = {}
    if sys.platform == "win32":
        adapter_cls: type[WindowsConsoleAdapter | WindowsPipeAdapter]
        if silent_shell_v2:
            adapter_cls = WindowsPipeAdapter
            # /Q and -Command - suppress raw OEM-encoded command echo. Without
            # this, capture receives a mixed CP936 echo plus UTF-8 child output.
            adapters["cmd"] = adapter_cls("cmd", "cmd.exe /D /Q")
            adapters["powershell"] = adapter_cls("powershell", "powershell.exe -NoLogo -NoExit -Command -")
        else:
            adapter_cls = WindowsConsoleAdapter
            adapters["cmd"] = adapter_cls("cmd", "cmd.exe", silent=silent_shell)
            adapters["powershell"] = adapter_cls("powershell", "powershell.exe -NoLogo -NoExit", silent=silent_shell)
        if shutil.which("pwsh.exe") or shutil.which("pwsh"):
            adapters["pwsh"] = adapter_cls("pwsh", "pwsh.exe -NoLogo -NoExit -Command -") if silent_shell_v2 else adapter_cls("pwsh", "pwsh.exe -NoLogo", silent=silent_shell)
        bash = find_git_bash()
        if bash:
            command = f'"{bash}" --login -i'
            adapters["mingw64"] = adapter_cls("mingw64", command) if silent_shell_v2 else adapter_cls("mingw64", command, silent=silent_shell)
        return adapters, "cmd"
    if sys.platform == "darwin":
        adapters["mac-pty"] = PosixPtyAdapter("mac-pty")
        adapters["mac-terminal"] = MacTerminalAdapter()
        return adapters, "mac-pty"
    adapters["posix-pty"] = PosixPtyAdapter("posix-pty")
    return adapters, "posix-pty"


class ReverseClientRuntime:
    def __init__(
        self,
        identifier: str,
        default_profile: str | None = None,
        silent_shell: bool = False,
        silent_shell_v2: bool = False,
        allow_http_ports: tuple[int, ...] = (),
    ):
        self.identifier = identifier
        self.client_id = str(uuid.uuid4())
        self.adapters, detected_default = build_adapters(silent_shell=silent_shell, silent_shell_v2=silent_shell_v2)
        self.default_profile = default_profile or detected_default
        self.sessions: dict[str, ClientSession] = {}
        # Local ports other clients may reach. Empty means this client cannot
        # be connected to at all — the safe default.
        self.allow_http_ports = frozenset(allow_http_ports)

    def http_port_allowed(self, port: int) -> bool:
        return port in self.allow_http_ports

    def registration_payload(self, *, replace: bool) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "replace": replace,
            "platform": platform.system().lower() or sys.platform,
            "default_profile": self.default_profile,
            "profiles": [adapter.profile_info() for adapter in self.adapters.values()],
            "version": __version__,
            "client_id": self.client_id,
            "allow_http_ports": sorted(self.allow_http_ports),
        }

    def handle(self, message_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if message_type == "session.create":
            return self._create(payload)
        if message_type == "session.send_keys":
            return self._send_keys(payload)
        if message_type == "session.capture":
            return self._capture(payload)
        if message_type == "session.kill":
            return self._kill(payload)
        if message_type == "session.list":
            return {"sessions": [session.to_dict() for session in self.sessions.values()]}
        if message_type == "file.stat":
            return self._file_stat(payload)
        if message_type == "file.read":
            return self._file_read(payload)
        if message_type == "file.write":
            return self._file_write(payload)
        if message_type == "file.mkdir":
            return self._file_mkdir(payload)
        if message_type == "file.delete":
            return self._file_delete(payload)
        if message_type == "file.rename":
            return self._file_rename(payload)
        if message_type == "file.list":
            return self._file_list(payload)
        if message_type == "file.archive":
            return self._file_archive(payload)
        if message_type == "file.apply_archive":
            return self._file_apply_archive(payload)
        raise BridgeError(f"unsupported bridge message type {message_type!r}")

    def _create(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("name") or "")
        profile = str(payload.get("profile") or "")
        if not name:
            raise BridgeError("session name is required")
        if name in self.sessions:
            raise BridgeError(f"session {name!r} already exists", kind="already_exists")
        adapter = self.adapters.get(profile)
        if adapter is None:
            raise BridgeError(f"profile {profile!r} is not supported", kind="bad_profile")
        session = adapter.create(
            name=name,
            cmd=payload.get("cmd"),
            cwd=payload.get("cwd"),
            created_at=str(payload.get("created_at") or ""),
        )
        self.sessions[name] = session
        return {"session": session.to_dict()}

    def _get(self, name: str) -> ClientSession:
        session = self.sessions.get(name)
        if session is None:
            raise BridgeError(f"session {name!r} not found", kind="not_found")
        if not session.is_running():
            returncode = session.process.returncode if session.process is not None else None
            detail = ""
            if returncode is not None:
                detail = f" (returncode={returncode}"
                if returncode < 0:
                    try:
                        detail += f", signal={signal.Signals(-returncode).name}"
                    except ValueError:
                        pass
                detail += ")"
            raise BridgeError(f"session {name!r} is no longer running{detail}", kind="session_gone")
        return session

    def _send_keys(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._get(str(payload.get("session") or ""))
        keys = payload.get("keys") if isinstance(payload.get("keys"), list) else []
        flags = payload.get("flags") if isinstance(payload.get("flags"), list) else []
        if session.adapter is None:
            raise BridgeError("session has no adapter", kind="session_gone")
        session.adapter.send_keys(session, [str(k) for k in keys], [str(f) for f in flags])
        return {"ok": True}

    def _capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        session = self._get(str(payload.get("session") or ""))
        if session.adapter is None:
            raise BridgeError("session has no adapter", kind="session_gone")
        lines = int(payload.get("lines") or 0)
        if lines <= 0:
            raise BridgeError("--lines must be a positive integer")
        ansi = bool(payload.get("ansi"))
        return session.adapter.capture(session, lines, ansi).to_dict()

    def _kill(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("session") or "")
        # Do NOT route through _get(): it raises session_gone for an already-exited
        # session, which would prevent cleanup. kill must succeed — clearing the
        # record and freeing adapter resources (pty fd, etc.) — whether the
        # underlying process is still running or already exited.
        session = self.sessions.get(name)
        if session is None:
            raise BridgeError(f"session {name!r} not found", kind="not_found")
        if session.adapter is not None and session.is_running():
            session.adapter.kill(session)
        self.sessions.pop(name, None)
        return {"ok": True}

    def _file_path(self, payload: dict[str, Any]) -> Path:
        raw = str(payload.get("path") or "")
        if not raw:
            raise BridgeError("file path is required", kind="bad_request")
        try:
            validate_bridge_path(raw)
        except Exception as e:
            raise BridgeError(str(e), kind="bad_request")
        return Path(raw).expanduser()

    def _file_stat(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._file_path(payload)
        if path.is_symlink() or not path.exists():
            return {"exists": False, "type": "none", "size": 0}
        if path.is_file():
            return {"exists": True, "type": "file", "size": path.stat().st_size}
        if path.is_dir():
            return {"exists": True, "type": "dir", "size": 0}
        return {"exists": True, "type": "other", "size": path.stat().st_size}

    def _file_read(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._file_path(payload)
        if not path.is_file():
            raise BridgeError(f"not a regular file: {path}", kind="not_found")
        data = path.read_bytes()
        return {
            "content_b64": base64.b64encode(data).decode("ascii"),
            "size": len(data),
        }

    def _file_write(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._file_path(payload)
        content_b64 = str(payload.get("content_b64") or "")
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (ValueError, base64.binascii.Error) as e:
            raise BridgeError(f"invalid base64 payload: {e}", kind="bad_request")
        max_size = int(payload.get("max_size") or 0) or 0
        if max_size and len(data) > max_size:
            raise BridgeError(
                f"file size {len(data)} exceeds bridge limit {max_size}", kind="too_large"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        mode_value = payload.get("mode")
        if isinstance(mode_value, int) and 0 <= mode_value <= 0o777:
            try:
                os.chmod(path, mode_value)
            except OSError:
                pass
        return {"size": len(data)}

    def _file_mkdir(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._file_path(payload)
        path.mkdir(parents=True, exist_ok=True)
        return {"ok": True}

    def _file_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._file_path(payload)
        if not path.exists():
            raise BridgeError(f"path does not exist: {path}", kind="not_found")
        if path.is_file():
            entries = [{
                "path": str(path),
                "type": "file",
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }]
            return {"entries": entries}
        if not path.is_dir():
            raise BridgeError(f"not a directory: {path}", kind="bad_request")
        recursive = bool(payload.get("recursive", True))
        entries: list[dict[str, Any]] = []
        if recursive:
            children = sorted(path.rglob("*"))
        else:
            children = sorted(path.iterdir())
        for child in children:
            if child.is_symlink():
                entries.append({"path": str(child), "type": "symlink", "size": 0})
                continue
            if child.is_file():
                entries.append({
                    "path": str(child),
                    "type": "file",
                    "size": child.stat().st_size,
                    "sha256": _sha256_file(child),
                })
            elif child.is_dir():
                entries.append({"path": str(child), "type": "dir", "size": 0})
        return {"entries": entries}

    def _file_archive(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._file_path(payload)
        if not path.is_dir():
            raise BridgeError(f"not a directory: {path}", kind="bad_request")
        rel_paths = payload.get("rel_paths") if isinstance(payload.get("rel_paths"), list) else []
        dirs = payload.get("dirs") if isinstance(payload.get("dirs"), list) else []
        max_size = int(payload.get("max_size") or DEFAULT_BRIDGE_MAX_ARCHIVE_SIZE)
        try:
            archive_b64 = build_sync_archive_b64(
                path,
                [str(p) for p in rel_paths],
                [str(d) for d in dirs],
                max_archive_size=max_size,
            )
        except Exception as e:
            raise BridgeError(str(e), kind="bad_request")
        return {"archive_b64": archive_b64, "size": len(base64.b64decode(archive_b64 or ""))}

    def _file_apply_archive(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._file_path(payload)
        archive_b64 = str(payload.get("archive_b64") or "")
        delete_paths = payload.get("delete_paths") if isinstance(payload.get("delete_paths"), list) else []
        max_size = int(payload.get("max_size") or DEFAULT_BRIDGE_MAX_ARCHIVE_SIZE)
        try:
            result = apply_sync_archive_b64(
                path,
                archive_b64,
                delete_paths=[str(p) for p in delete_paths],
                max_archive_size=max_size,
            )
        except Exception as e:
            raise BridgeError(str(e), kind="bad_request")
        return result

    def _file_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self._file_path(payload)
        recursive = bool(payload.get("recursive", False))
        if not path.exists():
            # Idempotent: a missing target is already "deleted" (mirrors `rm -f`).
            return {"ok": True, "existed": False}
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            if not recursive:
                raise BridgeError(
                    f"path is a directory; pass recursive to remove it: {path}",
                    kind="bad_request",
                )
            shutil.rmtree(path)
        else:
            path.unlink()
        return {"ok": True, "existed": True}

    def _file_rename(self, payload: dict[str, Any]) -> dict[str, Any]:
        src = self._file_path({"path": payload.get("src", "")})
        dst = self._file_path({"path": payload.get("dst", "")})
        if not src.exists():
            raise BridgeError(f"source path does not exist: {src}", kind="not_found")
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        return {"ok": True, "src": str(src), "dst": str(dst)}


def _post_json(url: str, token: str, payload: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_events(url: str, token: str, identifier: str):
    query = urllib.parse.urlencode({"identifier": identifier})
    req = urllib.request.Request(f"{url.rstrip('/')}/client/events?{query}", method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "text/event-stream")
    return urllib.request.urlopen(req, timeout=None)


def _iter_sse(response) -> Any:
    data_lines: list[str] = []
    for raw in response:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                joined = "\n".join(data_lines)
                data_lines = []
                yield json.loads(joined)
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


class _EventStreamReader:
    def __init__(self, response) -> None:
        self._response = response
        self._queue: queue.Queue[dict[str, Any] | BaseException | object] = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def get(self, timeout: float) -> dict[str, Any] | BaseException | object:
        return self._queue.get(timeout=timeout)

    def _run(self) -> None:
        try:
            for event in _iter_sse(self._response):
                self._queue.put(event)
            self._queue.put(_STREAM_EOF)
        except Exception as e:
            self._queue.put(e)


def _handle_event(runtime: ReverseClientRuntime, base: str, token: str, event: dict[str, Any]) -> None:
    request_id = str(event.get("id") or "")
    message_type = str(event.get("type") or "")
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    # Streaming file operations run on their own outbound data connection in a
    # separate thread so they never block the control loop (which must keep
    # servicing session.send-keys / capture / other commands mid-transfer).
    if message_type == "file.open_read":
        _handle_open_read(runtime, base, token, request_id, payload)
        return
    if message_type == "file.open_write":
        _handle_open_write(runtime, base, token, request_id, payload)
        return
    # A port tunnel lives as long as the connection it carries, so it also runs
    # off the control loop — otherwise one open browser tab would block every
    # other command this client serves.
    if message_type == "http_port.open":
        _handle_http_port_open(runtime, base, token, request_id, payload)
        return
    if _is_logged_bridge_command(message_type):
        logger.info(
            "bridge client received command identifier={} request_id={} type={} details={}",
            runtime.identifier,
            request_id,
            message_type,
            _bridge_command_summary(message_type, payload),
        )
    try:
        result = runtime.handle(message_type, payload)
        response_payload = {"id": request_id, "ok": True, "payload": result}
        if _is_logged_bridge_command(message_type):
            logger.info(
                "bridge client completed command identifier={} request_id={} type={} result_keys={}",
                runtime.identifier,
                request_id,
                message_type,
                sorted(result) if isinstance(result, dict) else [],
            )
    except BridgeError as e:
        response_payload = {
            "id": request_id,
            "ok": False,
            "error": {"kind": e.kind, "message": e.message, "code": e.exit_code},
        }
        logger.warning(
            "bridge client command failed identifier={} request_id={} type={} kind={} code={} message={}",
            runtime.identifier,
            request_id,
            message_type,
            e.kind,
            e.exit_code,
            e.message,
        )
    except Exception as e:  # pragma: no cover - defensive path for remote clients
        response_payload = {
            "id": request_id,
            "ok": False,
            "error": {"kind": "client_error", "message": str(e), "code": 1},
        }
        logger.exception(
            "bridge client command crashed identifier={} request_id={} type={}",
            runtime.identifier,
            request_id,
            message_type,
        )
    _post_json(f"{base}/client/result", token, response_payload)


def _data_plane_target(base: str) -> tuple[http.client.HTTPConnection, str]:
    """Open a connection for the client's own data endpoints, with the base path.

    The broker URL may carry a path prefix — ``https://host/aimux_bridge`` is a
    documented form, and it is how the Mobius gateway exposes the broker. The
    data endpoints live *under* that prefix, so dropping it (as building
    ``/client/data`` by hand does) sends the request somewhere that is not the
    broker at all. Returns the connection and the prefix to prepend.
    """
    parsed = urllib.parse.urlsplit(base)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=None)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=None)
    return conn, parsed.path.rstrip("/")


def _stream_file_upload(base: str, token: str, request_id: str, path: Path, runtime: ReverseClientRuntime) -> None:
    """Stream a local file up to the broker over the remote's own outbound data
    connection (``POST /client/data``) in 1 MiB chunks. Runs on a dedicated
    thread so the control loop stays free. On failure, reports the error via
    ``/client/result`` so the broker's read-stream handler can surface it."""
    conn: http.client.HTTPConnection | None = None
    try:
        conn, base_path = _data_plane_target(base)
        size = path.stat().st_size
        conn.putrequest("POST", f"{base_path}/client/data?id={urllib.parse.quote(request_id)}")
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                conn.send(chunk)
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            # Without this check any non-broker reply (a gateway 404, say) looks
            # like a successful upload, and the far side waits out its timeout
            # instead of reporting what actually went wrong.
            raise BridgeError(
                f"data stream error {response.status}: {body.decode('utf-8', errors='replace')}",
                kind="remote_disconnected",
            )
        logger.info(
            "bridge client streamed file identifier={} request_id={} size={}",
            runtime.identifier,
            request_id,
            size,
        )
    except Exception as exc:  # pragma: no cover - network path
        logger.exception("bridge client file stream failed identifier={} request_id={}", runtime.identifier, request_id)
        _post_json(
            f"{base}/client/result",
            token,
            {"id": request_id, "ok": False, "error": {"kind": "client_error", "message": str(exc), "code": 1}},
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _handle_open_read(runtime: ReverseClientRuntime, base: str, token: str, request_id: str, payload: dict[str, Any]) -> None:
    """Handle ``file.open_read``: validate the path, then stream it up on a
    separate thread. Validation errors are reported synchronously via
    ``/client/result``; success hands off to the data-connection thread."""
    try:
        path = runtime._file_path(payload)
        if not path.is_file():
            raise BridgeError(f"not a regular file: {path}", kind="not_found")
    except BridgeError as exc:
        _post_json(
            f"{base}/client/result",
            token,
            {"id": request_id, "ok": False, "error": {"kind": exc.kind, "message": exc.message, "code": exc.exit_code}},
        )
        return
    threading.Thread(
        target=_stream_file_upload,
        args=(base, token, request_id, path, runtime),
        daemon=True,
    ).start()


def _stream_file_download(base: str, token: str, request_id: str, path: Path, runtime: ReverseClientRuntime) -> None:
    """Pull a file down from the broker over the remote's own outbound data
    connection (``GET /client/data``) in 1 MiB chunks, writing to disk as it
    arrives. Reports the write result via ``/client/result`` when done."""
    conn: http.client.HTTPConnection | None = None
    try:
        conn, base_path = _data_plane_target(base)
        conn.putrequest("GET", f"{base_path}/client/data?id={urllib.parse.quote(request_id)}")
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Accept", "application/octet-stream")
        conn.endheaders()
        response = conn.getresponse()
        if response.status != 200:
            body = response.read().decode("utf-8", errors="replace")
            raise BridgeError(f"data stream error {response.status}: {body}", kind="remote_disconnected")
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with open(path, "wb") as handle:
            while True:
                chunk = response.read(STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                handle.write(chunk)
                size += len(chunk)
        _post_json(f"{base}/client/result", token, {"id": request_id, "ok": True, "payload": {"size": size}})
        logger.info(
            "bridge client received file identifier={} request_id={} size={}",
            runtime.identifier,
            request_id,
            size,
        )
    except Exception as exc:  # pragma: no cover - network path
        logger.exception("bridge client file download failed identifier={} request_id={}", runtime.identifier, request_id)
        _post_json(
            f"{base}/client/result",
            token,
            {"id": request_id, "ok": False, "error": {"kind": "client_error", "message": str(exc), "code": 1}},
        )
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _handle_open_write(runtime: ReverseClientRuntime, base: str, token: str, request_id: str, payload: dict[str, Any]) -> None:
    """Handle ``file.open_write``: validate the path, then pull the file on a
    separate thread. Validation errors are reported synchronously."""
    try:
        path = runtime._file_path(payload)
    except BridgeError as exc:
        _post_json(
            f"{base}/client/result",
            token,
            {"id": request_id, "ok": False, "error": {"kind": exc.kind, "message": exc.message, "code": exc.exit_code}},
        )
        return
    threading.Thread(
        target=_stream_file_download,
        args=(base, token, request_id, path, runtime),
        daemon=True,
    ).start()


def _handle_http_port_open(runtime: ReverseClientRuntime, base: str, token: str, request_id: str, payload: dict[str, Any]) -> None:
    """Handle ``http_port.open``: dial the requested local port, then relay.

    The dial and the relay both belong off the control loop, so the whole
    handshake runs on a dedicated thread.
    """
    try:
        port = int(payload.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    session_id = str(payload.get("session_id") or "")
    threading.Thread(
        target=_run_http_port_tunnel_guarded,
        args=(runtime, base, token, request_id, session_id, port),
        daemon=True,
    ).start()


def _run_http_port_tunnel_guarded(
    runtime: ReverseClientRuntime,
    base: str,
    token: str,
    request_id: str,
    session_id: str,
    port: int,
) -> None:
    """Report an unexpected crash back to the broker.

    The handshake runs on its own thread, so without this a bug here would
    surface only as a 30s timeout on the far side — with no hint as to why.
    """
    try:
        _run_http_port_tunnel(runtime, base, token, request_id, session_id, port)
    except Exception as exc:  # pragma: no cover - defensive path
        logger.exception(
            "bridge client http port tunnel crashed identifier={} session={} port={}",
            runtime.identifier,
            session_id,
            port,
        )
        _post_json(
            f"{base}/client/result",
            token,
            {"id": request_id, "ok": False, "error": {"kind": "client_error", "message": str(exc), "code": 1}},
        )


def _run_http_port_tunnel(
    runtime: ReverseClientRuntime,
    base: str,
    token: str,
    request_id: str,
    session_id: str,
    port: int,
) -> None:
    """Dial ``127.0.0.1:<port>`` and pump it through the broker's tunnel.

    Only loopback is ever dialled: the port owner decides *which* ports may be
    reached, and never *what* they resolve to, so a tunnel can't be turned into
    a jump host onto the rest of the network.
    """
    if not runtime.http_port_allowed(port):
        logger.warning(
            "bridge client refused http port tunnel identifier={} session={} port={} allow_http_ports={}",
            runtime.identifier,
            session_id,
            port,
            sorted(runtime.allow_http_ports) or "none",
        )
        _post_json(
            f"{base}/client/result",
            token,
            {
                "id": request_id,
                "ok": False,
                "error": {
                    "kind": "port_not_allowed",
                    "message": f"port {port} is not in this client's --allow-http-ports",
                    "code": 1,
                },
            },
        )
        return

    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=port_tunnel.TUNNEL_DIAL_TIMEOUT)
        sock.settimeout(None)
    except OSError as exc:
        _post_json(
            f"{base}/client/result",
            token,
            {
                "id": request_id,
                "ok": False,
                "error": {
                    "kind": "port_unreachable",
                    "message": f"127.0.0.1:{port} is not reachable on this machine: {exc}",
                    "code": 2,
                },
            },
        )
        return

    # Report success only after the local dial worked, so the reroute side can
    # tell "nothing listening there" apart from "the tunnel stalled" without
    # waiting out a timeout.
    _post_json(
        f"{base}/client/result",
        token,
        {"id": request_id, "ok": True, "payload": {"session_id": session_id}},
    )
    logger.info(
        "bridge client opened http port tunnel identifier={} session={} port={}",
        runtime.identifier,
        session_id,
        port,
    )
    try:
        port_tunnel.relay_socket_over_bridge(
            base=base,
            token=token,
            session_id=session_id,
            sock=sock,
            prefix="/client",
        )
    finally:
        try:
            sock.close()
        except OSError:
            pass
        logger.info(
            "bridge client closed http port tunnel identifier={} session={}",
            runtime.identifier,
            session_id,
        )


def _serve_event_stream(
    response,
    *,
    runtime: ReverseClientRuntime,
    base: str,
    token: str,
    stop_event: threading.Event | None,
    poll_interval: float,
    heartbeat_interval: float = BRIDGE_CLIENT_HEARTBEAT_INTERVAL,
) -> None:
    reader = _EventStreamReader(response)
    reader.start()
    last_heartbeat = time.monotonic()
    while True:
        if stop_event is not None and stop_event.is_set():
            raise KeyboardInterrupt
        now = time.monotonic()
        if heartbeat_interval > 0 and now - last_heartbeat >= heartbeat_interval:
            logger.info(
                "bridge client heartbeat identifier={} sessions={} profiles={}",
                runtime.identifier,
                len(runtime.sessions),
                ",".join(runtime.adapters),
            )
            last_heartbeat = now
        try:
            item = reader.get(timeout=poll_interval)
        except queue.Empty:
            continue
        if item is _STREAM_EOF:
            return
        if isinstance(item, BaseException):
            raise item
        _handle_event(runtime, base, token, item)


# Reconnect backoff: quick retries first (ride out brief blips), then a fixed
# 60s cadence. Total ~30 min before giving up — long enough to survive a broker
# restart, a network drop, or a remote host rebooting mid long-running command.
# User-requested: keep retrying for at least 30 minutes, max 1 minute between
# attempts (was 3 retries / ~35s, gave up too easily and killed remote commands).
RECONNECT_INTERVALS: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0) + (60.0,) * 29


def _register_with_retry(
    base: str,
    token: str,
    payload: dict[str, Any],
    *,
    identifier: str,
    reconnect: bool,
    retry_intervals: tuple[float, ...] | None = None,
    sleep=time.sleep,
) -> dict[str, Any]:
    """POST /client/register, retrying on 409 Conflict or transient errors.

    A 409 means the broker still believes a previous client with this
    identifier is connected (its disconnect has not been observed yet, e.g.
    a half-open TCP connection). Rather than crashing for an external
    supervisor to restart us, back off and retry so we take over the moment
    the stale record clears. Auth failures (401/403) are not retried.
    """
    intervals = retry_intervals if retry_intervals is not None else REGISTER_RETRY_INTERVALS
    attempt = 0
    while True:
        try:
            response = _post_json(f"{base}/client/register", token, payload)
            if attempt:
                logger.info("bridge register recovered identifier={} after {} retries", identifier, attempt)
            return response
        except urllib.error.HTTPError as e:
            reason = f"HTTP {e.code} {e.reason}"
            if e.code in (401, 403):
                raise BridgeError(
                    f"bridge register rejected: {reason}",
                    kind="auth_required",
                    exit_code=4,
                )
            retryable = e.code == 409
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            reason = str(e) or e.__class__.__name__
            retryable = True

        if not reconnect or not retryable:
            raise BridgeError(
                f"bridge register failed: {reason}",
                kind="remote_disconnected",
                exit_code=2,
            )
        if attempt >= len(intervals):
            raise BridgeError(
                f"bridge register failed after {len(intervals)} retries: {reason}",
                kind="remote_disconnected",
                exit_code=2,
            )
        delay = intervals[attempt]
        logger.warning(
            "bridge register failed identifier={} reason={} retry {}/{} in {:g}s",
            identifier,
            reason,
            attempt + 1,
            len(intervals),
            delay,
        )
        sleep(delay)
        attempt += 1


def connect(
    url: str,
    *,
    identifier: str,
    token: str,
    default_profile: str | None = None,
    replace: bool = False,
    reconnect: bool = True,
    silent_shell: bool = False,
    silent_shell_v2: bool = False,
    allow_http_ports: tuple[int, ...] = (),
    reconnect_intervals: tuple[float, ...] | None = None,
    register_retry_intervals: tuple[float, ...] | None = None,
    sleep=time.sleep,
    stop_event: threading.Event | None = None,
    stream_poll_interval: float = 0.2,
    heartbeat_interval: float = BRIDGE_CLIENT_HEARTBEAT_INTERVAL,
    watchdog_path: str | os.PathLike[str] | None = None,
    watchdog_timeout: float = WATCHDOG_TIMEOUT,
    runtime_path: str | os.PathLike[str] | None = None,
) -> None:
    validate_identifier(identifier)
    runtime = ReverseClientRuntime(
        identifier,
        default_profile=default_profile,
        silent_shell=silent_shell,
        silent_shell_v2=silent_shell_v2,
        allow_http_ports=allow_http_ports,
    )
    base = url.rstrip("/")
    payload = runtime.registration_payload(replace=replace)
    logged_registration = False
    watchdog_stop = threading.Event()
    runtime_file = Path(runtime_path) if runtime_path else None

    def runtime_update(**changes: Any) -> None:
        if runtime_file is None:
            return
        with RUNTIME_WRITE_LOCK:
            try:
                runtime_file.parent.mkdir(parents=True, exist_ok=True)
                state: dict[str, Any] = {}
                if runtime_file.exists():
                    try:
                        state = json.loads(runtime_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        state = {}
                state.update(changes)
                tmp = runtime_file.with_name(runtime_file.name + f".tmp-{os.getpid()}")
                tmp.write_text(json.dumps(state, separators=(",", ":"), ensure_ascii=True), encoding="utf-8")
                os.replace(tmp, runtime_file)
            except OSError as exc:
                logger.warning("runtime state write failed path={} error={}", runtime_file, exc)

    runtime_update(pid=os.getpid(), launched_at=int(time.time() * 1000), last_feed_watchdog=int(time.time() * 1000), realtime_healthy_display=False, need_external_restart=False)

    def watchdog_loop() -> None:
        path = Path(watchdog_path) if watchdog_path else None
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.touch(exist_ok=True)
        except OSError as exc:
            logger.warning("watchdog unavailable path={} error={}", path, exc)
            return
        while not watchdog_stop.wait(WATCHDOG_INTERVAL):
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = watchdog_timeout + 1
            if age > watchdog_timeout:
                logger.info("watchdog expired path={} age={:.1f}s, stopping AIMUX", path, age)
                runtime_update(realtime_healthy_display=False)
                watchdog_stop.set()
                return

    watchdog_thread = threading.Thread(target=watchdog_loop, name="aimux-watchdog", daemon=True)
    watchdog_thread.start()

    intervals = reconnect_intervals if reconnect_intervals is not None else RECONNECT_INTERVALS
    attempt = 0
    while True:
        if watchdog_stop.is_set():
            return
        disconnect_error: Exception | None
        try:
            register_response = _register_with_retry(
                base,
                token,
                payload,
                identifier=identifier,
                reconnect=reconnect,
                retry_intervals=register_retry_intervals,
                sleep=sleep,
            )
            if not logged_registration:
                logger.info("aimux bridge client version {}", __version__)
                server_version = (
                    str(register_response.get("server_version") or "")
                    if isinstance(register_response, dict)
                    else ""
                )
                client_mismatch = version_mismatch_message(__version__, server_version)
                if client_mismatch:
                    logger.warning("bridge client {} — {}", identifier, client_mismatch)
                logger.info(
                    "connected bridge remote {!r}; profiles={}; default={}; allow_http_ports={}",
                    identifier,
                    ",".join(runtime.adapters),
                    runtime.default_profile,
                    sorted(runtime.allow_http_ports) or "none",
                )
                logged_registration = True
            else:
                logger.info("bridge remote {!r} re-registered before reconnecting event stream", identifier)
            with _open_events(base, token, identifier) as response:
                runtime_update(realtime_healthy_display=True, last_healthy_heartbeat=int(time.time() * 1000), need_external_restart=False)
                _serve_event_stream(
                    response,
                    runtime=runtime,
                    base=base,
                    token=token,
                    stop_event=stop_event,
                    poll_interval=stream_poll_interval,
                    heartbeat_interval=heartbeat_interval,
                )
            disconnect_error = OSError("bridge server closed the event stream")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            disconnect_error = e
            runtime_update(realtime_healthy_display=False)

        if not reconnect:
            raise BridgeError(
                f"bridge connection lost: {disconnect_error}",
                kind="remote_disconnected",
                exit_code=2,
            )
        if watchdog_stop.is_set():
            return
        if attempt >= len(intervals):
            raise BridgeError(
                f"bridge connection lost after {len(intervals)} retries: {disconnect_error}",
                kind="remote_disconnected",
                exit_code=2,
            )
        delay = intervals[attempt]
        logger.warning(
            "bridge connection lost ({}); retry {}/{} in {:g}s",
            disconnect_error,
            attempt + 1,
            len(intervals),
            delay,
        )
        sleep(delay)
        attempt += 1
