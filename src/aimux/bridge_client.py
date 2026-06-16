"""Reverse bridge client and terminal profile adapters."""
from __future__ import annotations

import base64
import json
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .bridge import BridgeError, validate_identifier
from .transfer import validate_bridge_path


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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

        command = cmd or self.command or os.environ.get("SHELL") or "/bin/sh"
        master_fd, slave_fd = pty.openpty()
        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")

        def setup_child_tty() -> None:
            if hasattr(os, "setsid"):
                os.setsid()
            try:
                import fcntl
                import termios

                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)
            except Exception:
                pass

        process = subprocess.Popen(
            command,
            shell=True,
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=setup_child_tty if hasattr(os, "setsid") else None,
            close_fds=True,
        )
        os.close(slave_fd)
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

    def __init__(self, name: str, command: str):
        self.name = name
        self.command = command

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
        process = subprocess.Popen(command, cwd=cwd, creationflags=creationflags)
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


def build_adapters() -> tuple[dict[str, TerminalAdapter], str]:
    adapters: dict[str, TerminalAdapter] = {}
    if sys.platform == "win32":
        adapters["cmd"] = WindowsConsoleAdapter("cmd", "cmd.exe")
        adapters["powershell"] = WindowsConsoleAdapter("powershell", "powershell.exe -NoLogo -NoExit")
        if shutil.which("pwsh.exe") or shutil.which("pwsh"):
            adapters["pwsh"] = WindowsConsoleAdapter("pwsh", "pwsh.exe -NoLogo")
        bash = find_git_bash()
        if bash:
            adapters["mingw64"] = WindowsConsoleAdapter("mingw64", f'"{bash}" --login -i')
        return adapters, "cmd"
    if sys.platform == "darwin":
        adapters["mac-pty"] = PosixPtyAdapter("mac-pty")
        adapters["mac-terminal"] = MacTerminalAdapter()
        return adapters, "mac-pty"
    adapters["posix-pty"] = PosixPtyAdapter("posix-pty")
    return adapters, "posix-pty"


class ReverseClientRuntime:
    def __init__(self, identifier: str, default_profile: str | None = None):
        self.identifier = identifier
        self.adapters, detected_default = build_adapters()
        self.default_profile = default_profile or detected_default
        self.sessions: dict[str, ClientSession] = {}

    def registration_payload(self, *, replace: bool) -> dict[str, Any]:
        return {
            "identifier": self.identifier,
            "replace": replace,
            "platform": platform.system().lower() or sys.platform,
            "default_profile": self.default_profile,
            "profiles": [adapter.profile_info() for adapter in self.adapters.values()],
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
        if message_type == "file.list":
            return self._file_list(payload)
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
            raise BridgeError(f"session {name!r} is no longer running", kind="session_gone")
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
        session = self._get(name)
        if session.adapter is not None:
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
            entries = [{"path": str(path), "type": "file", "size": path.stat().st_size}]
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
                entries.append({"path": str(child), "type": "file", "size": child.stat().st_size})
            elif child.is_dir():
                entries.append({"path": str(child), "type": "dir", "size": 0})
        return {"entries": entries}


def _post_json(url: str, token: str, payload: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


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


def connect(
    url: str,
    *,
    identifier: str,
    token: str,
    default_profile: str | None = None,
    replace: bool = False,
    reconnect: bool = True,
) -> None:
    validate_identifier(identifier)
    runtime = ReverseClientRuntime(identifier, default_profile=default_profile)
    base = url.rstrip("/")
    payload = runtime.registration_payload(replace=replace)
    _post_json(f"{base}/client/register", token, payload)
    print(
        f"connected bridge remote {identifier!r}; "
        f"profiles={','.join(runtime.adapters)}; default={runtime.default_profile}",
        flush=True,
    )

    while True:
        try:
            with _open_events(base, token, identifier) as response:
                for event in _iter_sse(response):
                    request_id = str(event.get("id") or "")
                    message_type = str(event.get("type") or "")
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    try:
                        result = runtime.handle(message_type, payload)
                        response_payload = {"id": request_id, "ok": True, "payload": result}
                    except BridgeError as e:
                        response_payload = {
                            "id": request_id,
                            "ok": False,
                            "error": {"kind": e.kind, "message": e.message, "code": e.exit_code},
                        }
                    except Exception as e:  # pragma: no cover - defensive path for remote clients
                        response_payload = {
                            "id": request_id,
                            "ok": False,
                            "error": {"kind": "client_error", "message": str(e), "code": 1},
                        }
                    _post_json(f"{base}/client/result", token, response_payload)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if not reconnect:
                raise BridgeError(f"bridge connection lost: {e}", kind="remote_disconnected", exit_code=2)
            time.sleep(2)
