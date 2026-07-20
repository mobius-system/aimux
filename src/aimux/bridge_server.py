"""HTTP/SSE broker for aimux bridge remotes."""
from __future__ import annotations

import json
import os
import queue
import secrets
import socket
import sys
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from loguru import logger

from . import __version__
from . import port_forward
from . import ssh as ssh_mod
from .bridge import (
    BridgeError,
    BridgeRuntime,
    clear_runtime,
    control_url_for_bind,
    external_host_hint,
    utc_now,
    validate_identifier,
    version_mismatch_message,
    write_runtime,
)


BRIDGE_SERVER_HEARTBEAT_LOG_INTERVAL = 180.0

# TCP keepalive tuning so the broker detects half-open reverse clients
# (Windows sleep / VPN switch / network drop) within tens of seconds instead
# of waiting for the kernel retransmit timeout (~minutes). The 15s SSE
# heartbeat write alone cannot detect this: data sits in the kernel send
# buffer and write() returns successfully while the peer is long gone.
BRIDGE_TCP_KEEPIDLE = 10
BRIDGE_TCP_KEEPINTVL = 3
BRIDGE_TCP_KEEPCNT = 3


def _apply_tcp_keepalive(sock) -> None:
    """Enable TCP keepalive on an accepted socket with short probe intervals.

    Best-effort and platform-defensive: the broker normally runs on Linux,
    but every option is guarded so a missing constant or a read-only socket
    never breaks request handling.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        return
    for opt_name, value in (
        ("TCP_KEEPIDLE", BRIDGE_TCP_KEEPIDLE),
        ("TCP_KEEPINTVL", BRIDGE_TCP_KEEPINTVL),
        ("TCP_KEEPCNT", BRIDGE_TCP_KEEPCNT),
    ):
        opt = getattr(socket, opt_name, None)
        if opt is not None:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass
    if sys.platform == "win32":
        try:
            sock.ioctl(
                socket.SIO_KEEPALIVE_VALS,
                (1, BRIDGE_TCP_KEEPIDLE * 1000, BRIDGE_TCP_KEEPINTVL * 1000),
            )
        except (OSError, AttributeError):
            pass


@dataclass
class PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    remote: str = ""
    message_type: str = ""


@dataclass
class ClientInfo:
    name: str
    platform: str
    default_profile: str | None
    profiles: list[dict[str, Any]]
    connected: bool = False
    last_seen: str | None = None
    client_version: str = ""
    client_id: str = ""
    queue: queue.Queue[dict[str, Any] | None] | None = None


class BridgeBrokerState:
    def __init__(self, token: str):
        self.token = token
        self.clients: dict[str, ClientInfo] = {}
        self.sessions: dict[tuple[str, str], dict[str, Any]] = {}
        self.pending: dict[str, PendingRequest] = {}
        self.lock = threading.RLock()

    def register_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload.get("identifier") or "")
        validate_identifier(name)
        if ssh_mod.host_exists(name):
            raise BridgeError(f"bridge identifier {name!r} conflicts with existing ssh host", kind="name_conflict")
        replace = bool(payload.get("replace"))
        platform = str(payload.get("platform") or "unknown")
        default_profile = payload.get("default_profile")
        profiles = payload.get("profiles") if isinstance(payload.get("profiles"), list) else []
        client_version = str(payload.get("version") or "")
        client_id = str(payload.get("client_id") or "")
        mismatch = version_mismatch_message(client_version, __version__)
        if mismatch:
            logger.warning("bridge client {} registered with {}", name, mismatch)
        with self.lock:
            existing = self.clients.get(name)
            if existing and existing.connected and not replace:
                raise BridgeError(f"bridge client {name!r} is already connected", kind="name_conflict")
            if existing and existing.queue is not None:
                try:
                    existing.queue.put_nowait(None)
                except queue.Full:
                    pass
                self._fail_pending_locked(name, reason="client replaced")
            same_client_process = bool(existing and existing.client_id and client_id and existing.client_id == client_id)
            self.clients[name] = ClientInfo(
                name=name,
                platform=platform,
                default_profile=str(default_profile) if default_profile else None,
                profiles=[p for p in profiles if isinstance(p, dict)],
                connected=False,
                last_seen=utc_now(),
                client_version=client_version,
                client_id=client_id,
            )
            stale_keys: list[tuple[str, str]] = []
            if not same_client_process:
                # A reverse client re-registering with a new client_id means its
                # process restarted, so its in-memory session table is now empty.
                # Drop cached records for this remote; otherwise `new` reports
                # "already exists" for sessions the client no longer has.
                stale_keys = [key for key in self.sessions if key[0] == name]
                for key in stale_keys:
                    self.sessions.pop(key, None)
        logger.info(
            "bridge client registered name={!r} platform={} default_profile={} profiles={} replace={} version={}",
            name,
            platform,
            default_profile,
            [p.get("name") for p in profiles if isinstance(p, dict)],
            replace,
            client_version or "unknown",
        )
        if stale_keys:
            logger.info(
                "bridge client re-registration cleared {} stale session(s) for name={!r}",
                len(stale_keys),
                name,
            )
        return {"ok": True, "server_version": __version__, "version_mismatch": bool(mismatch)}

    def connect_events(self, name: str) -> queue.Queue[dict[str, Any] | None]:
        validate_identifier(name)
        with self.lock:
            client = self.clients.get(name)
            if client is None:
                raise BridgeError(f"bridge client {name!r} is not registered", kind="not_registered")
            if client.queue is not None:
                try:
                    client.queue.put_nowait(None)
                except queue.Full:
                    pass
            q: queue.Queue[dict[str, Any] | None] = queue.Queue()
            client.queue = q
            client.connected = True
            client.last_seen = utc_now()
            logger.info("bridge client event stream connected name={!r}", name)
            return q

    def disconnect_events(self, name: str, q: queue.Queue[dict[str, Any] | None]) -> None:
        with self.lock:
            client = self.clients.get(name)
            if client and client.queue is q:
                client.connected = False
                client.queue = None
                client.last_seen = utc_now()
                failed = self._fail_pending_locked(name, reason="event stream disconnected")
                logger.info("bridge client event stream disconnected name={!r}", name)
                if failed:
                    logger.warning(
                        "bridge broker failed {} pending request(s) for disconnected client name={!r}",
                        failed,
                        name,
                    )

    def touch(self, name: str) -> None:
        """Record a heartbeat observation so last_seen reflects live SSE traffic."""
        with self.lock:
            client = self.clients.get(name)
            if client is not None:
                client.last_seen = utc_now()

    def list_remotes(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = []
            for client in sorted(self.clients.values(), key=lambda item: item.name):
                rows.append(
                    {
                        "name": client.name,
                        "type": "bridge",
                        "status": "connected" if client.connected else "disconnected",
                        "platform": client.platform,
                        "default_profile": client.default_profile,
                        "profiles": client.profiles,
                        "last_seen": client.last_seen,
                        "client_version": client.client_version,
                        "server_version": __version__,
                        "version_mismatch": bool(version_mismatch_message(client.client_version, __version__)),
                    }
                )
            return rows

    def connection_status(self, name: str) -> dict[str, Any]:
        validate_identifier(name)
        with self.lock:
            client = self.clients.get(name)
            connected = bool(client and client.connected and client.queue is not None)
            return {
                "identifier": name,
                "registered": client is not None,
                "event_stream_connected": connected,
                "last_seen": client.last_seen if client else None,
            }

    def list_sessions(self) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(v) for _, v in sorted(self.sessions.items())]

    def _drop_session(self, remote: str, name: str, *, reason: str) -> None:
        """Remove a cached session record so broker state stays in sync with the
        reverse client. Used when the client reports a session is gone, and when
        the client process restarts (its in-memory session table is then empty)."""
        with self.lock:
            existed = self.sessions.pop((remote, name), None) is not None
        if existed:
            logger.info(
                "bridge broker dropped stale session remote={!r} name={!r} reason={}",
                remote,
                name,
                reason,
            )

    def client_supports_profile(self, remote: str, profile: str) -> bool:
        with self.lock:
            client = self.clients.get(remote)
            if not client:
                return False
            for item in client.profiles:
                if item.get("name") == profile and item.get("available", True):
                    return True
            return False

    def submit(self, remote: str, message_type: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self.lock:
            client = self.clients.get(remote)
            if client is None:
                raise BridgeError(f"bridge remote {remote!r} not found", kind="not_found")
            if not client.connected or client.queue is None:
                raise BridgeError(f"bridge remote {remote!r} is disconnected", kind="remote_disconnected", exit_code=2)
            request_id = str(uuid.uuid4())
            pending = PendingRequest(remote=remote, message_type=message_type)
            self.pending[request_id] = pending
            client.queue.put({"id": request_id, "type": message_type, "payload": payload})
            logger.info("bridge broker queued request remote={!r} request_id={} type={}", remote, request_id, message_type)

        if not pending.event.wait(timeout):
            with self.lock:
                self.pending.pop(request_id, None)
            logger.warning(
                "bridge broker request timed out remote={!r} request_id={} type={} timeout={}",
                remote,
                request_id,
                message_type,
                timeout,
            )
            raise BridgeError(f"bridge request to {remote!r} timed out", kind="timeout", exit_code=124)

        result = pending.result or {}
        if not result.get("ok"):
            error = result.get("error") if isinstance(result.get("error"), dict) else {}
            message = str(error.get("message") or "bridge client request failed")
            kind = str(error.get("kind") or "client_error")
            exit_code = int(error.get("code") or 1)
            logger.warning(
                "bridge broker request failed remote={!r} request_id={} type={} kind={} code={} message={}",
                remote,
                request_id,
                message_type,
                kind,
                exit_code,
                message,
            )
            raise BridgeError(message, kind=kind, exit_code=exit_code)
        logger.info("bridge broker request completed remote={!r} request_id={} type={}", remote, request_id, message_type)
        return result.get("payload") if isinstance(result.get("payload"), dict) else {}

    def complete(self, payload: dict[str, Any]) -> None:
        request_id = str(payload.get("id") or "")
        with self.lock:
            pending = self.pending.pop(request_id, None)
        if pending is not None:
            pending.result = payload
            pending.event.set()
            logger.info("bridge broker received result request_id={} ok={}", request_id, bool(payload.get("ok")))
        else:
            logger.warning("bridge broker received result for unknown request_id={}", request_id)

    def _fail_pending_locked(self, remote: str, *, reason: str) -> int:
        failed = 0
        for request_id, pending in list(self.pending.items()):
            if pending.remote != remote:
                continue
            self.pending.pop(request_id, None)
            pending.result = {
                "id": request_id,
                "ok": False,
                "error": {
                    "kind": "remote_disconnected",
                    "message": f"bridge remote {remote!r} disconnected while handling {pending.message_type}: {reason}",
                    "code": 2,
                },
            }
            pending.event.set()
            failed += 1
        return failed

    def create_session(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        remote = str(payload.get("remote") or "")
        name = str(payload.get("name") or "")
        profile = str(payload.get("profile") or "")
        if not remote or not name or not profile:
            raise BridgeError("remote, name, and profile are required")
        if "/" in name:
            raise BridgeError("--name must not contain '/'")
        if not self.client_supports_profile(remote, profile):
            raise BridgeError(f"remote {remote!r} does not support profile {profile!r}", kind="bad_profile")
        with self.lock:
            if (remote, name) in self.sessions:
                raise BridgeError(f"session {remote!r}/{name!r} already exists", kind="already_exists")
        response = self.submit(remote, "session.create", payload, timeout)
        session = response.get("session") if isinstance(response.get("session"), dict) else {}
        record = {
            "id": f"{remote}/{name}",
            "location": remote,
            "name": name,
            "backend": "bridge",
            "transport": "bridge",
            "host": remote,
            "profile": profile,
            "status": str(session.get("status") or "detached"),
            "client_status": "connected",
            "created_at": payload.get("created_at"),
            "created_by": payload.get("created_by"),
            "cmd": payload.get("cmd"),
            "cwd": payload.get("cwd"),
            "pid": session.get("pid"),
        }
        with self.lock:
            self.sessions[(remote, name)] = record
        logger.info("bridge broker recorded session remote={!r} name={!r} profile={!r}", remote, name, profile)
        return {"session": record}

    def send_keys(self, remote: str, name: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self.lock:
            if (remote, name) not in self.sessions:
                raise BridgeError(f"session {remote!r}/{name!r} not found", kind="not_found")
        payload = dict(payload)
        payload["session"] = name
        try:
            return self.submit(remote, "session.send_keys", payload, timeout)
        except BridgeError as e:
            if e.kind in ("not_found", "session_gone"):
                self._drop_session(remote, name, reason=f"client reports {e.kind}")
            raise

    def capture(self, remote: str, name: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        with self.lock:
            if (remote, name) not in self.sessions:
                raise BridgeError(f"session {remote!r}/{name!r} not found", kind="not_found")
        payload = dict(payload)
        payload["session"] = name
        try:
            return self.submit(remote, "session.capture", payload, timeout)
        except BridgeError as e:
            if e.kind in ("not_found", "session_gone"):
                self._drop_session(remote, name, reason=f"client reports {e.kind}")
            raise

    def kill_session(self, remote: str, name: str, timeout: float) -> dict[str, Any]:
        with self.lock:
            if (remote, name) not in self.sessions:
                raise BridgeError(f"session {remote!r}/{name!r} not found", kind="not_found")
        try:
            result = self.submit(remote, "session.kill", {"session": name}, timeout)
        except BridgeError as e:
            # Client no longer knows this session — the kill's goal (destroy it)
            # is already met. Drop our stale cache and treat as success so callers
            # can recover from a half-dead record without restarting the broker.
            if e.kind in ("not_found", "session_gone"):
                self._drop_session(remote, name, reason=f"client reports {e.kind} on kill")
                return {"ok": True}
            raise
        self._drop_session(remote, name, reason="killed")
        return result or {"ok": True}

    def file_op(self, remote: str, action: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if action not in {"stat", "read", "write", "mkdir", "list", "archive", "apply_archive"}:
            raise BridgeError(f"unknown file action {action!r}", kind="bad_request")
        return self.submit(remote, f"file.{action}", payload, timeout)


def make_handler(state: BridgeBrokerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "aimux-bridge/0.1"
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:  # noqa: D401
            super().setup()
            _apply_tcp_keepalive(self.connection)

        def do_GET(self) -> None:  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/api/health":
                    self._require_auth()
                    self._json(HTTPStatus.OK, {"ok": True})
                    return
                if parsed.path == "/api/remotes":
                    self._require_auth()
                    self._json(HTTPStatus.OK, {"remotes": state.list_remotes()})
                    return
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "remotes"] and parts[3] == "connection":
                    self._require_auth()
                    self._json(HTTPStatus.OK, state.connection_status(urllib.parse.unquote(parts[2])))
                    return
                if parsed.path == "/api/sessions":
                    self._require_auth()
                    self._json(HTTPStatus.OK, {"sessions": state.list_sessions()})
                    return
                if parsed.path == port_forward.FORWARD_PATH:
                    self._require_auth()
                    port_forward.handle_bridge_forward(self)
                    return
                if parsed.path == "/client/events":
                    self._require_auth()
                    query = urllib.parse.parse_qs(parsed.query)
                    identifier = (query.get("identifier") or [""])[0]
                    self._events(identifier)
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": {"kind": "not_found", "message": "not found"}})
            except BridgeError as e:
                self._bridge_error(e)

        def do_POST(self) -> None:  # noqa: N802
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == port_forward.FORWARD_PATH:
                    self._require_auth()
                    port_forward.handle_bridge_forward(self)
                    return
                payload = self._read_json()
                if parsed.path == "/client/register":
                    self._require_auth()
                    self._json(HTTPStatus.OK, state.register_client(payload))
                    return
                if parsed.path == "/client/result":
                    self._require_auth()
                    state.complete(payload)
                    self._json(HTTPStatus.OK, {"ok": True})
                    return
                if parsed.path == "/api/sessions":
                    self._require_auth()
                    timeout = float(payload.pop("timeout", 30) or 30)
                    self._json(HTTPStatus.OK, state.create_session(payload, timeout))
                    return

                parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
                if len(parts) == 5 and parts[:2] == ["api", "sessions"]:
                    self._require_auth()
                    remote, name, action = parts[2], parts[3], parts[4]
                    timeout = float(payload.pop("timeout", 30) or 30)
                    if action == "send-keys":
                        self._json(HTTPStatus.OK, state.send_keys(remote, name, payload, timeout))
                        return
                    if action == "capture":
                        self._json(HTTPStatus.OK, state.capture(remote, name, payload, timeout))
                        return
                    if action == "kill":
                        self._json(HTTPStatus.OK, state.kill_session(remote, name, timeout))
                        return
                if len(parts) == 5 and parts[:2] == ["api", "remotes"] and parts[3] == "files":
                    self._require_auth()
                    remote, action = parts[2], parts[4]
                    timeout = float(payload.pop("timeout", 60) or 60)
                    self._json(HTTPStatus.OK, state.file_op(remote, action, payload, timeout))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": {"kind": "not_found", "message": "not found"}})
            except BridgeError as e:
                self._bridge_error(e)
            except ValueError as e:
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"kind": "bad_request", "message": str(e)}})

        def do_PUT(self) -> None:  # noqa: N802
            self._forward_method()

        def do_PATCH(self) -> None:  # noqa: N802
            self._forward_method()

        def do_DELETE(self) -> None:  # noqa: N802
            self._forward_method()

        def do_HEAD(self) -> None:  # noqa: N802
            self._forward_method()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._forward_method()

        def _forward_method(self) -> None:
            try:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != port_forward.FORWARD_PATH:
                    self._json(HTTPStatus.NOT_FOUND, {"error": {"kind": "not_found", "message": "not found"}})
                    return
                self._require_auth()
                port_forward.handle_bridge_forward(self)
            except BridgeError as e:
                self._bridge_error(e)
            except ValueError as e:
                self._json(HTTPStatus.BAD_REQUEST, {"error": {"kind": "bad_request", "message": str(e)}})

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _require_auth(self) -> None:
            got = self.headers.get("Authorization", "")
            if got != f"Bearer {state.token}":
                raise BridgeError("bridge token is invalid", kind="auth_required", exit_code=4)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            if not raw:
                return {}
            data = json.loads(raw.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _events(self, identifier: str) -> None:
            q = state.connect_events(identifier)
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                last_heartbeat_log = time.monotonic()
                while True:
                    try:
                        item = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        state.touch(identifier)
                        now = time.monotonic()
                        if now - last_heartbeat_log >= BRIDGE_SERVER_HEARTBEAT_LOG_INTERVAL:
                            logger.info("bridge server heartbeat identifier={!r}", identifier)
                            last_heartbeat_log = now
                        continue
                    if item is None:
                        break
                    payload = json.dumps(item, ensure_ascii=False)
                    frame = f"id: {item.get('id', '')}\nevent: message\ndata: {payload}\n\n"
                    self.wfile.write(frame.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                state.disconnect_events(identifier, q)

        def _bridge_error(self, e: BridgeError) -> None:
            status = HTTPStatus.BAD_REQUEST
            if e.exit_code == 2:
                status = HTTPStatus.BAD_GATEWAY
            elif e.exit_code == 4:
                status = HTTPStatus.UNAUTHORIZED
            elif e.exit_code == 124:
                status = HTTPStatus.GATEWAY_TIMEOUT
            elif e.kind == "not_found":
                status = HTTPStatus.NOT_FOUND
            elif e.kind == "name_conflict" or e.kind == "already_exists":
                status = HTTPStatus.CONFLICT
            self._json(status, {"error": {"kind": e.kind, "message": e.message, "code": e.exit_code}})

    return Handler


def serve(host: str, port: int, *, token: str | None = None) -> None:
    token = token or secrets.token_urlsafe(24)
    state = BridgeBrokerState(token)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    actual_port = int(httpd.server_address[1])
    control_url = control_url_for_bind(host, actual_port)
    runtime = BridgeRuntime(
        url=control_url,
        token=token,
        pid=os.getpid(),
        host=host,
        port=actual_port,
        created_at=utc_now(),
    )
    write_runtime(runtime)
    connect_host = external_host_hint(host)
    logger.info("aimux bridge server version {}", __version__)
    logger.info("bridge listening on {}:{}", host, actual_port)
    logger.info(
        "connect client with: aimux reverse connect http://{}:{} --identifier <name> --token {}",
        connect_host,
        actual_port,
        token,
    )
    try:
        httpd.serve_forever()
    finally:
        logger.info("bridge server shutting down pid={}", os.getpid())
        clear_runtime()
        httpd.server_close()
