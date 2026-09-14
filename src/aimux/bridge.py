"""Bridge runtime discovery and control-client helpers."""
from __future__ import annotations

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


RUNTIME_ENV = "AIMUX_BRIDGE_RUNTIME"
# Streaming file transfers relay raw bytes in 1 MiB chunks; the chunk size also
# sets the granularity of the progress callback.
STREAM_CHUNK_SIZE = 1024 * 1024
# progress(done_bytes, total_bytes)
ProgressCallback = Callable[[int, int], None]
RUNTIME_DIR_ENV = "AIMUX_RUNTIME_DIR"


class BridgeError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "bridge_error", exit_code: int = 1):
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.exit_code = exit_code


class BridgeAuthError(BridgeError):
    def __init__(self, message: str):
        super().__init__(message, kind="auth_required", exit_code=4)


class BridgeUnreachableError(BridgeError):
    def __init__(self, message: str):
        super().__init__(message, kind="remote_unreachable", exit_code=2)


class BridgeTimeoutError(BridgeError):
    def __init__(self, message: str):
        super().__init__(message, kind="timeout", exit_code=124)


def version_mismatch_message(client_version: str, server_version: str) -> str | None:
    """Return a warning string when client/server aimux versions differ, else None.

    Stays silent (returns None) when either side is unknown — e.g. an older
    client that did not report a version, or a server that did not echo one —
    so a version-skewed deployment never false-alarms on the missing side.
    """
    if not client_version or not server_version or client_version == server_version:
        return None
    return (
        f"aimux version mismatch: client={client_version!r} server={server_version!r}; "
        "bridge commands may fail with unexpected errors — upgrade the older side to match"
    )


@dataclass(frozen=True)
class BridgeRuntime:
    url: str
    token: str
    pid: int | None
    host: str
    port: int
    created_at: str | None = None


def runtime_path() -> Path:
    explicit = os.environ.get(RUNTIME_ENV)
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get(RUNTIME_DIR_ENV)
    root = Path(base).expanduser() if base else Path.home() / ".aimux"
    return root / "bridge" / "runtime.json"


def write_runtime(runtime: BridgeRuntime) -> Path:
    path = runtime_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(runtime.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    return path


def read_runtime() -> BridgeRuntime | None:
    path = runtime_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return BridgeRuntime(
            url=str(data["url"]).rstrip("/"),
            token=str(data["token"]),
            pid=int(data["pid"]) if data.get("pid") is not None else None,
            host=str(data.get("host") or ""),
            port=int(data["port"]),
            created_at=data.get("created_at"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear_runtime() -> None:
    try:
        runtime_path().unlink()
    except FileNotFoundError:
        pass


def validate_identifier(identifier: str) -> None:
    if not identifier:
        raise BridgeError("bridge identifier must not be empty")
    if identifier == "local":
        raise BridgeError("bridge identifier must not be 'local'")
    if "/" in identifier:
        raise BridgeError("bridge identifier must not contain '/'")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
    bad = sorted({ch for ch in identifier if ch not in allowed})
    if bad:
        raise BridgeError(
            f"bridge identifier {identifier!r} contains unsupported character {bad[0]!r}; "
            "use letters, digits, '_', '.', or '-'"
        )


def control_url_for_bind(host: str, port: int) -> str:
    local_host = "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host
    if ":" in local_host and not local_host.startswith("["):
        local_host = f"[{local_host}]"
    return f"http://{local_host}:{port}"


def external_host_hint(host: str) -> str:
    if host in {"", "0.0.0.0", "::"}:
        try:
            return socket.gethostname()
        except OSError:
            return "<server-ip>"
    return host


def _request_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 5.0,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        payload_obj = _decode_error(raw)
        msg = payload_obj.get("message") or payload_obj.get("error") or raw or e.reason
        kind = str(payload_obj.get("kind") or "bridge_error")
        try:
            code = int(payload_obj.get("code") or 1)
        except (TypeError, ValueError):
            code = 1
        if e.code in (401, 403):
            raise BridgeAuthError(str(msg))
        if e.code == 504 or code == 124:
            raise BridgeTimeoutError(str(msg))
        raise BridgeError(str(msg), kind=kind, exit_code=code)
    except urllib.error.URLError as e:
        raise BridgeUnreachableError(f"bridge broker is unreachable: {e.reason}")
    except TimeoutError:
        raise BridgeTimeoutError("bridge broker request timed out")

    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise BridgeError(f"bridge broker returned invalid JSON: {e}")


def _request_bytes(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 180.0,
    progress: ProgressCallback | None = None,
    total: int = 0,
) -> bytes:
    """Like ``_request_json`` but returns the raw response body (used by the
    streaming file-read endpoint, whose response is ``application/octet-stream``
    rather than JSON). Shares the same error decoding as ``_request_json``.
    When ``progress`` is given, the response is read in 1 MiB chunks and the
    callback is invoked with ``(done_bytes, total_bytes)`` after each chunk."""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/octet-stream")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if progress is None:
                return resp.read()
            chunks: list[bytes] = []
            done = 0
            while True:
                chunk = resp.read(STREAM_CHUNK_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
                done += len(chunk)
                progress(done, total)
            return b"".join(chunks)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        payload_obj = _decode_error(raw)
        msg = payload_obj.get("message") or payload_obj.get("error") or raw or e.reason
        kind = str(payload_obj.get("kind") or "bridge_error")
        try:
            code = int(payload_obj.get("code") or 1)
        except (TypeError, ValueError):
            code = 1
        if e.code in (401, 403):
            raise BridgeAuthError(str(msg))
        if e.code == 504 or code == 124:
            raise BridgeTimeoutError(str(msg))
        raise BridgeError(str(msg), kind=kind, exit_code=code)
    except urllib.error.URLError as e:
        raise BridgeUnreachableError(f"bridge broker is unreachable: {e.reason}")
    except TimeoutError:
        raise BridgeTimeoutError("bridge broker request timed out")


def _decode_error(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"message": raw}
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return data["error"]
    if isinstance(data, dict):
        return data
    return {"message": raw}


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class BridgeControlClient:
    def __init__(self, runtime: BridgeRuntime):
        self.runtime = runtime

    @classmethod
    def from_runtime(cls) -> "BridgeControlClient":
        runtime = read_runtime()
        if runtime is None:
            raise BridgeUnreachableError("bridge broker is not running")
        return cls(runtime)

    def health(self, *, timeout: float = 2.0) -> dict[str, Any]:
        return _request_json("GET", f"{self.runtime.url}/api/health", self.runtime.token, timeout=timeout)

    def list_remotes(self, *, timeout: float = 2.0) -> list[dict[str, Any]]:
        data = _request_json("GET", f"{self.runtime.url}/api/remotes", self.runtime.token, timeout=timeout)
        remotes = data.get("remotes", [])
        return remotes if isinstance(remotes, list) else []

    def get_remote(self, name: str, *, timeout: float = 2.0) -> dict[str, Any] | None:
        for remote in self.list_remotes(timeout=timeout):
            if remote.get("name") == name:
                return remote
        return None

    def list_sessions(self, *, timeout: float = 2.0) -> list[dict[str, Any]]:
        data = _request_json("GET", f"{self.runtime.url}/api/sessions", self.runtime.token, timeout=timeout)
        sessions = data.get("sessions", [])
        return sessions if isinstance(sessions, list) else []

    def create_session(
        self,
        *,
        remote: str,
        name: str,
        profile: str,
        cmd: str | None,
        cwd: str | None,
        created_at: str,
        created_by: str,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/sessions",
            self.runtime.token,
            {
                "remote": remote,
                "name": name,
                "profile": profile,
                "cmd": cmd,
                "cwd": cwd,
                "created_at": created_at,
                "created_by": created_by,
            },
            timeout=timeout,
        )

    def send_keys(
        self,
        *,
        remote: str,
        name: str,
        keys: list[str],
        flags: list[str],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/sessions/{_quote(remote)}/{_quote(name)}/send-keys",
            self.runtime.token,
            {"keys": keys, "flags": flags},
            timeout=timeout,
        )

    def capture(
        self,
        *,
        remote: str,
        name: str,
        lines: int,
        ansi: bool,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/sessions/{_quote(remote)}/{_quote(name)}/capture",
            self.runtime.token,
            {"lines": lines, "ansi": ansi},
            timeout=timeout,
        )

    def kill_session(self, *, remote: str, name: str, timeout: float = 30.0) -> dict[str, Any]:
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/sessions/{_quote(remote)}/{_quote(name)}/kill",
            self.runtime.token,
            {},
            timeout=timeout,
        )

    def file_stat(self, *, remote: str, path: str, timeout: float = 60.0) -> dict[str, Any]:
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/stat",
            self.runtime.token,
            {"path": path},
            timeout=timeout,
        )

    def file_read(self, *, remote: str, path: str, timeout: float = 180.0) -> dict[str, Any]:
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/read",
            self.runtime.token,
            {"path": path},
            timeout=timeout,
        )

    def file_read_stream(
        self,
        *,
        remote: str,
        path: str,
        timeout: float = 180.0,
        progress: ProgressCallback | None = None,
        total: int = 0,
    ) -> bytes:
        """Read a file's raw bytes over the remote's independent data connection.

        Unlike ``file_read`` (which base64-encodes the whole file in one JSON
        message), this streams raw bytes in 1 MiB chunks, bounding memory on
        both sides. Returns the full contents as ``bytes``. ``progress`` is
        called with ``(done_bytes, total_bytes)`` after each chunk; ``total``
        is the expected size (from a prior ``file_stat``).
        """
        return _request_bytes(
            "POST",
            f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/read-stream",
            self.runtime.token,
            {"path": path},
            timeout=timeout,
            progress=progress,
            total=total,
        )

    def file_write_stream(
        self,
        *,
        remote: str,
        path: str,
        data: bytes,
        timeout: float = 180.0,
        progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Write raw bytes to a remote file over the remote's independent data
        connection (raw 1 MiB chunks, no base64). Returns the broker's JSON
        result ``{"ok": True, "size": ...}``. ``progress`` is called with
        ``(done_bytes, total_bytes)`` after each uploaded chunk.
        """
        url = f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/write-stream?path={_quote(path)}"
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path_part = parsed.path + (("?" + parsed.query) if parsed.query else "")
        cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        conn = cls(host, port, timeout=timeout)
        total = len(data)
        view = memoryview(data)
        status = 0
        raw = ""
        try:
            conn.putrequest("POST", path_part)
            conn.putheader("Authorization", f"Bearer {self.runtime.token}")
            conn.putheader("Content-Type", "application/octet-stream")
            conn.putheader("Content-Length", str(total))
            conn.endheaders()
            done = 0
            while done < total:
                chunk = view[done:done + STREAM_CHUNK_SIZE]
                if not chunk:
                    break
                conn.send(bytes(chunk))
                done += len(chunk)
                if progress is not None:
                    progress(done, total)
            response = conn.getresponse()
            status = response.status
            raw = response.read().decode("utf-8")
        except (http.client.HTTPException, OSError) as exc:
            raise BridgeUnreachableError(f"bridge broker is unreachable: {exc}") from exc
        finally:
            conn.close()
        if status != 200:
            payload_obj = _decode_error(raw)
            msg = payload_obj.get("message") or payload_obj.get("error") or raw or "request failed"
            kind = str(payload_obj.get("kind") or "bridge_error")
            try:
                code = int(payload_obj.get("code") or 1)
            except (TypeError, ValueError):
                code = 1
            if status in (401, 403):
                raise BridgeAuthError(str(msg))
            if status == 504 or code == 124:
                raise BridgeTimeoutError(str(msg))
            raise BridgeError(str(msg), kind=kind, exit_code=code)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeError(f"bridge broker returned invalid JSON: {exc}")

    def file_write(
        self,
        *,
        remote: str,
        path: str,
        content_b64: str,
        mode: int | None = None,
        max_size: int | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": path, "content_b64": content_b64}
        if mode is not None:
            payload["mode"] = int(mode)
        if max_size is not None:
            payload["max_size"] = int(max_size)
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/write",
            self.runtime.token,
            payload,
            timeout=timeout,
        )

    def file_mkdir(self, *, remote: str, path: str, timeout: float = 60.0) -> dict[str, Any]:
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/mkdir",
            self.runtime.token,
            {"path": path},
            timeout=timeout,
        )

    def file_delete(
        self,
        *,
        remote: str,
        path: str,
        recursive: bool = False,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/delete",
            self.runtime.token,
            {"path": path, "recursive": bool(recursive)},
            timeout=timeout,
        )

    def file_rename(
        self,
        *,
        remote: str,
        src: str,
        dst: str,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/rename",
            self.runtime.token,
            {"src": src, "dst": dst},
            timeout=timeout,
        )

    def file_list(
        self,
        *,
        remote: str,
        path: str,
        recursive: bool = True,
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/list",
            self.runtime.token,
            {"path": path, "recursive": recursive},
            timeout=timeout,
        )

    def file_archive(
        self,
        *,
        remote: str,
        path: str,
        rel_paths: list[str],
        dirs: list[str],
        max_size: int | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"path": path, "rel_paths": rel_paths, "dirs": dirs}
        if max_size is not None:
            payload["max_size"] = int(max_size)
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/archive",
            self.runtime.token,
            payload,
            timeout=timeout,
        )

    def file_apply_archive(
        self,
        *,
        remote: str,
        path: str,
        archive_b64: str,
        delete_paths: list[str],
        max_size: int | None = None,
        timeout: float = 180.0,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "path": path,
            "archive_b64": archive_b64,
            "delete_paths": delete_paths,
        }
        if max_size is not None:
            payload["max_size"] = int(max_size)
        return _request_json(
            "POST",
            f"{self.runtime.url}/api/remotes/{_quote(remote)}/files/apply_archive",
            self.runtime.token,
            payload,
            timeout=timeout,
        )


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
