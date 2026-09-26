"""HTTP/SSE broker for aimux bridge remotes."""
from __future__ import annotations

import base64
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
from typing import Any, Iterator

from loguru import logger

from . import __version__
from . import port_forward
from . import port_tunnel
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

# Streaming file transfer: chunks relayed between the CLI and the remote's
# independent data connection are bounded to 1 MiB so neither side ever holds
# a whole file in memory, and the reverse control SSE is never head-of-line
# blocked by bulk data.
STREAM_CHUNK_SIZE = 1024 * 1024
STREAM_OPEN_TIMEOUT = 60.0  # remote must open its data connection within this
STREAM_IDLE_TIMEOUT = 60.0  # max gap between chunks before we give up
# How much of a file a remote puts into one upload body. Small on purpose: a
# proxy sitting in front of the broker (nginx, with request buffering on by
# default) does not forward a request body until it has received all of it, so
# one big POST leaves the receiving side staring at nothing for the entire
# upload and then delivers it all at once. It also keeps every body under a
# proxy's default body size cap.
DATA_UPLOAD_SLICE_SIZE = 512 * 1024

# Client-to-client HTTP port tunnels. Each session is two independent byte
# pipes (one per direction) between two reverse clients, so the broker never
# holds more than TUNNEL_QUEUE_DEPTH chunks per direction in memory.
HTTP_TUNNEL_CHUNK_SIZE = port_tunnel.TUNNEL_CHUNK_SIZE
HTTP_TUNNEL_QUEUE_DEPTH = port_tunnel.TUNNEL_QUEUE_DEPTH
HTTP_TUNNEL_OPEN_TIMEOUT = 15.0
HTTP_TUNNEL_IDLE_TIMEOUT = port_tunnel.TUNNEL_IDLE_TIMEOUT
# Abandoned sessions (a client accepted locally but never opened its streams)
# are swept opportunistically whenever a new tunnel is created.
HTTP_TUNNEL_ABANDON_AFTER = 120.0
# How often an idle tunnel stream rechecks its exit conditions. Data never
# waits on this: the queue hands it over the moment it arrives.
TUNNEL_SSE_POLL = 0.2
# Comment frames keep intermediaries (nginx proxy_read_timeout and friends)
# from killing a tunnel that is legitimately idle.
TUNNEL_SSE_KEEPALIVE = 15.0
# A declared chunk size is attacker-controlled, so it is clamped before the
# read rather than trusted.
_MAX_TUNNEL_CHUNK_SIZE = 1024 * 1024
_MAX_CHUNK_LINE = 16 * 1024

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


def parse_allow_http_ports(value: Any) -> frozenset[int]:
    """Normalize a client's ``--allow-http-ports`` declaration.

    Anything malformed is ignored rather than rejected: an unparseable
    allowlist must fail closed (no port reachable), never open.
    """
    if not isinstance(value, (list, tuple, set, frozenset)):
        return frozenset()
    ports: set[int] = set()
    for item in value:
        try:
            port = int(item)
        except (TypeError, ValueError):
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return frozenset(ports)


def _iter_chunked(rfile: Any) -> Iterator[bytes]:
    """Yield the payloads of a chunked request body.

    ``BaseHTTPRequestHandler`` does not decode chunked bodies for us, so the
    framing is parsed here. A truncated body simply ends the stream — the
    caller treats that as EOF, which is what a vanished client means.
    """
    while True:
        line = rfile.readline(_MAX_CHUNK_LINE)
        if not line:
            return
        size_text = line.split(b";", 1)[0].strip()
        if not size_text:
            continue
        try:
            size = int(size_text, 16)
        except ValueError:
            raise BridgeError("invalid chunked request body", kind="bad_request")
        if size == 0:
            while True:  # consume trailers up to the blank line
                trailer = rfile.readline(_MAX_CHUNK_LINE)
                if not trailer or trailer in (b"\r\n", b"\n"):
                    return
        if size > _MAX_TUNNEL_CHUNK_SIZE:
            # The size line is attacker-controlled; without this a bogus value
            # turns into a huge read.
            raise BridgeError("chunked request body chunk is too large", kind="bad_request")
        payload = rfile.read(size)
        if len(payload) != size:
            return
        terminator = rfile.read(1)
        if terminator == b"\r":
            rfile.read(1)  # the LF
        elif terminator != b"\n":  # bare LF is tolerated, anything else is corrupt
            return
        if payload:
            yield payload


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
    # Local ports this client allows *other* clients to reach. Empty means the
    # client cannot be connected to at all — the safe default.
    allow_http_ports: frozenset[int] = frozenset()


@dataclass
class TunnelPipe:
    """One direction of a tunnel.

    Payload rides a bounded queue, so a slow reader throttles the producer
    instead of growing the broker's heap. The end-of-stream and abandoned
    signals are deliberately *out-of-band* events rather than queue sentinels:
    a sentinel would have to be forced into a full queue, and the only way to
    make room would be to drop a payload chunk — which truncates the byte
    stream silently.
    """

    chunks: queue.Queue[bytes] = field(default_factory=lambda: queue.Queue(maxsize=HTTP_TUNNEL_QUEUE_DEPTH))
    producer_done: threading.Event = field(default_factory=threading.Event)
    abandoned: threading.Event = field(default_factory=threading.Event)


@dataclass
class HttpPortTunnel:
    """One client-to-client byte pipe pair, keyed by session id.

    ``up`` carries bytes from the reroute side to the target owner (its POST
    body in, its SSE stream out); ``down`` carries the response bytes back.
    """

    session_id: str
    remote: str
    port: int
    created_at: str
    up: TunnelPipe = field(default_factory=TunnelPipe)
    down: TunnelPipe = field(default_factory=TunnelPipe)
    up_finished: bool = False
    down_finished: bool = False
    last_activity: float = field(default_factory=time.monotonic)
    streams_open: int = 0

    def pipe(self, direction: str) -> TunnelPipe:
        return self.up if direction == "up" else self.down


def _put_with_backpressure(pipe: TunnelPipe, chunk: bytes) -> bool:
    """Enqueue a chunk, waiting while the reader falls behind.

    Returns ``False`` if the tunnel was abandoned while we waited: the reader
    is gone, so nothing will ever drain this direction and the caller must stop
    rather than block forever holding a handler thread.
    """
    while not pipe.abandoned.is_set():
        try:
            pipe.chunks.put(chunk, timeout=TUNNEL_SSE_POLL)
            return True
        except queue.Full:
            continue
    return False


class BridgeBrokerState:
    def __init__(self, token: str):
        self.token = token
        self.clients: dict[str, ClientInfo] = {}
        self.sessions: dict[tuple[str, str], dict[str, Any]] = {}
        self.pending: dict[str, PendingRequest] = {}
        # request_id -> (data_q, ready_event, result_q). data_q carries chunks
        # (bytes | None EOF | dict error); ready_event (write only) is set by the
        # remote's GET /client/data; result_q (write only) carries the remote's
        # write outcome so it never races the data chunks on data_q.
        self._streams: dict[str, tuple[queue.Queue, threading.Event | None, queue.Queue | None]] = {}
        # request_id -> number of the next upload slice expected on a read
        # stream. Slices carry their number so a reordered upload fails loudly
        # instead of silently splicing the file together wrong.
        self.read_seq: dict[str, int] = {}
        # session_id -> HttpPortTunnel. See create_http_port_tunnel.
        self.http_tunnels: dict[str, HttpPortTunnel] = {}
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
        allow_http_ports = parse_allow_http_ports(payload.get("allow_http_ports"))
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
                self.close_http_tunnels_for_remote(name, reason="client replaced")
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
                allow_http_ports=allow_http_ports,
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
            "bridge client registered name={!r} platform={} default_profile={} profiles={} replace={} version={} allow_http_ports={}",
            name,
            platform,
            default_profile,
            [p.get("name") for p in profiles if isinstance(p, dict)],
            replace,
            client_version or "unknown",
            sorted(allow_http_ports) or "none",
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
                # A reverse client that drops takes its reachable ports with it;
                # ending the tunnels here lets the reroute side notice promptly
                # instead of waiting out its idle timeout.
                self.close_http_tunnels_for_remote(name, reason="target client disconnected")
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
                        "allow_http_ports": sorted(client.allow_http_ports),
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
            stream = self._streams.get(request_id)
        if pending is not None:
            pending.result = payload
            pending.event.set()
            logger.info("bridge broker received result request_id={} ok={}", request_id, bool(payload.get("ok")))
            return
        if stream is not None:
            # A streaming request reports its outcome here. Reads report only
            # failures (their success is the byte stream itself) — forwarded on
            # the data queue. Writes report both success and failure on a
            # dedicated result queue, which the write-stream handler awaits
            # without racing the data chunks.
            q, _ready, result_q = stream
            (result_q if result_q is not None else q).put(payload)
            logger.info(
                "bridge broker received stream result request_id={} ok={}",
                request_id,
                bool(payload.get("ok")),
            )
            return
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
        if action not in {"stat", "read", "write", "mkdir", "delete", "rename", "list", "archive", "apply_archive"}:
            raise BridgeError(f"unknown file action {action!r}", kind="bad_request")
        return self.submit(remote, f"file.{action}", payload, timeout)

    # -- streaming file transfer (independent data connection) ----------------

    def open_read_stream(self, remote: str, path: str) -> tuple[str, queue.Queue]:
        """Ask the remote to open a data connection and stream a file to us.

        Returns ``(request_id, q)`` where ``q`` receives ``bytes`` chunks, a
        ``None`` EOF sentinel, or a ``dict`` error payload. Fire-and-forget on
        the control SSE: the remote streams over its own outbound connection
        so bulk data never blocks the control channel.
        """
        with self.lock:
            client = self.clients.get(remote)
            if client is None:
                raise BridgeError(f"bridge remote {remote!r} not found", kind="not_found")
            if not client.connected or client.queue is None:
                raise BridgeError(f"bridge remote {remote!r} is disconnected", kind="remote_disconnected", exit_code=2)
            request_id = str(uuid.uuid4())
            q: queue.Queue = queue.Queue()
            self._streams[request_id] = (q, None, None)
            self.read_seq[request_id] = 0
            client.queue.put(
                {
                    "id": request_id,
                    "type": "file.open_read",
                    # Advertised so the remote chops its upload up. Clients
                    # that predate this key ignore it and send one body.
                    "payload": {"path": path, "slice_size": DATA_UPLOAD_SLICE_SIZE},
                }
            )
            logger.info(
                "bridge broker opened read stream remote={!r} request_id={} path={!r}",
                remote,
                request_id,
                path,
            )
        return request_id, q

    def open_write_stream(self, remote: str, path: str, size: int) -> tuple[str, queue.Queue, threading.Event, queue.Queue]:
        """Ask the remote to open a data connection and pull a file we will send.

        Mirrors ``open_read_stream`` but for the send (broker->remote) direction:
        the remote opens a ``GET /client/data``, signals readiness via an event,
        then consumes chunks we relay from the CLI's upload body. The write
        outcome is delivered on a separate result queue so it never races the
        data chunks.
        """
        with self.lock:
            client = self.clients.get(remote)
            if client is None:
                raise BridgeError(f"bridge remote {remote!r} not found", kind="not_found")
            if not client.connected or client.queue is None:
                raise BridgeError(f"bridge remote {remote!r} is disconnected", kind="remote_disconnected", exit_code=2)
            request_id = str(uuid.uuid4())
            q: queue.Queue = queue.Queue()
            ready = threading.Event()
            result_q: queue.Queue = queue.Queue()
            self._streams[request_id] = (q, ready, result_q)
            client.queue.put(
                {"id": request_id, "type": "file.open_write", "payload": {"path": path, "size": size}}
            )
            logger.info(
                "bridge broker opened write stream remote={!r} request_id={} path={!r} size={}",
                remote,
                request_id,
                path,
                size,
            )
        return request_id, q, ready, result_q

    def get_stream(self, request_id: str) -> tuple[queue.Queue, threading.Event | None, queue.Queue | None] | None:
        with self.lock:
            return self._streams.get(request_id)

    def claim_read_slice(self, request_id: str, seq: int) -> bool:
        """Reserve upload slice ``seq`` of a read stream, if it is the next one."""
        with self.lock:
            expected = self.read_seq.get(request_id)
            if expected is None or seq != expected:
                return False
            self.read_seq[request_id] = seq + 1
            return True

    def close_stream(self, request_id: str) -> None:
        with self.lock:
            self._streams.pop(request_id, None)
            self.read_seq.pop(request_id, None)

    # -- client-to-client http port tunnels -----------------------------------

    def create_http_port_tunnel(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Open a byte pipe pair from the caller to ``remote_client``'s port.

        The allowlist is enforced here as well as on the target client, so an
        unauthorized port is refused without waking the far side at all.
        """
        remote = str(payload.get("remote_client") or "")
        if not remote:
            raise BridgeError("remote_client is required", kind="bad_request")
        try:
            port = int(payload.get("port") or 0)
        except (TypeError, ValueError):
            raise BridgeError("port must be an integer", kind="bad_request")
        if not 1 <= port <= 65535:
            raise BridgeError("port must be between 1 and 65535", kind="bad_request")

        with self.lock:
            self._sweep_http_tunnels_locked()
            client = self.clients.get(remote)
            if client is None:
                raise BridgeError(f"bridge remote {remote!r} not found", kind="not_found")
            if not client.connected or client.queue is None:
                raise BridgeError(f"bridge remote {remote!r} is disconnected", kind="remote_disconnected", exit_code=2)
            if port not in client.allow_http_ports:
                allowed = ", ".join(str(p) for p in sorted(client.allow_http_ports))
                detail = f"allowed ports: {allowed}" if allowed else "it declared no --allow-http-ports"
                raise BridgeError(
                    f"remote {remote!r} does not allow connections to port {port} ({detail})",
                    kind="port_not_allowed",
                )
            session_id = str(uuid.uuid4())
            tunnel = HttpPortTunnel(session_id=session_id, remote=remote, port=port, created_at=utc_now())
            self.http_tunnels[session_id] = tunnel

        try:
            self.submit(remote, "http_port.open", {"port": port, "session_id": session_id}, timeout)
        except BridgeError:
            self.close_http_tunnel(session_id, reason="target refused or failed to open the tunnel")
            raise
        logger.info(
            "bridge broker opened http port tunnel session={} remote={!r} port={}",
            session_id,
            remote,
            port,
        )
        return {
            "session": {
                "id": session_id,
                "remote_client": remote,
                "port": port,
                "created_at": tunnel.created_at,
            }
        }

    def get_http_tunnel(self, session_id: str) -> HttpPortTunnel | None:
        with self.lock:
            return self.http_tunnels.get(session_id)

    def list_http_tunnels(self) -> list[dict[str, Any]]:
        with self.lock:
            return [
                {
                    "id": tunnel.session_id,
                    "remote_client": tunnel.remote,
                    "port": tunnel.port,
                    "created_at": tunnel.created_at,
                    "streams_open": tunnel.streams_open,
                }
                for tunnel in sorted(self.http_tunnels.values(), key=lambda item: item.created_at or "")
            ]

    def note_tunnel_activity(self, session_id: str) -> None:
        with self.lock:
            tunnel = self.http_tunnels.get(session_id)
            if tunnel is not None:
                tunnel.last_activity = time.monotonic()

    def finish_tunnel_direction(self, session_id: str, direction: str) -> None:
        """Mark one direction's producer as done so its reader can see EOF.

        A tunnel is torn down only once *both* directions have finished, so the
        surviving half can still drain what the peer already sent.
        """
        with self.lock:
            tunnel = self.http_tunnels.get(session_id)
            if tunnel is None:
                return
            tunnel.pipe(direction).producer_done.set()
            if direction == "up":
                tunnel.up_finished = True
            else:
                tunnel.down_finished = True
            both_done = tunnel.up_finished and tunnel.down_finished
        if both_done:
            self.close_http_tunnel(session_id, reason="both directions finished")

    def close_http_tunnel(self, session_id: str, *, reason: str = "") -> None:
        """Tear a session down and wake everyone blocked on it.

        Setting ``abandoned`` is what unblocks a producer stuck on a full
        queue; without it that handler thread would wait for a reader that will
        never come back.
        """
        with self.lock:
            tunnel = self.http_tunnels.pop(session_id, None)
            if tunnel is not None:
                for pipe in (tunnel.up, tunnel.down):
                    pipe.producer_done.set()
                    pipe.abandoned.set()
        if tunnel is not None:
            logger.info(
                "bridge broker closed http port tunnel session={} remote={!r} port={} reason={}",
                session_id,
                tunnel.remote,
                tunnel.port,
                reason or "closed",
            )

    def close_http_tunnels_for_remote(self, remote: str, *, reason: str) -> int:
        with self.lock:
            session_ids = [sid for sid, tunnel in self.http_tunnels.items() if tunnel.remote == remote]
        for session_id in session_ids:
            self.close_http_tunnel(session_id, reason=reason)
        return len(session_ids)

    def _sweep_http_tunnels_locked(self) -> None:
        """Reclaim sessions nobody ever attached streams to.

        Called under the lock from create_http_port_tunnel — a cheap sweep that
        keeps abandoned handshakes (local connection accepted, far side never
        followed through) from accumulating forever.
        """
        now = time.monotonic()
        stale = [
            session_id
            for session_id, tunnel in self.http_tunnels.items()
            if tunnel.streams_open == 0 and now - tunnel.last_activity > HTTP_TUNNEL_ABANDON_AFTER
        ]
        for session_id in stale:
            tunnel = self.http_tunnels.pop(session_id, None)
            if tunnel is not None:
                for pipe in (tunnel.up, tunnel.down):
                    pipe.producer_done.set()
                    pipe.abandoned.set()
                logger.warning(
                    "bridge broker swept abandoned http port tunnel session={} remote={!r} port={}",
                    session_id,
                    tunnel.remote,
                    tunnel.port,
                )


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
                if parsed.path == "/api/http-ports":
                    self._require_auth()
                    self._json(HTTPStatus.OK, {"sessions": state.list_http_tunnels()})
                    return
                if parsed.path in ("/api/http-ports/stream", "/client/http-ports/stream"):
                    self._require_auth()
                    query = urllib.parse.parse_qs(parsed.query)
                    session_id = (query.get("id") or [""])[0]
                    direction = (query.get("direction") or [""])[0]
                    # The /api family serves the reroute side (it reads the
                    # target's bytes), the /client family the port owner.
                    expected = "down" if parsed.path.startswith("/api/") else "up"
                    if direction != expected:
                        raise BridgeError(f"direction must be {expected!r} for {parsed.path}", kind="bad_request")
                    self._http_tunnel_sse(session_id, direction)
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
                if parsed.path == "/client/data":
                    self._require_auth()
                    query = urllib.parse.parse_qs(parsed.query)
                    request_id = (query.get("id") or [""])[0]
                    self._data_download(request_id)
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
                if parsed.path == "/client/data":
                    self._require_auth()
                    query = urllib.parse.parse_qs(parsed.query)
                    request_id = (query.get("id") or [""])[0]
                    raw_seq = (query.get("seq") or [""])[0]
                    seq: int | None = None
                    if raw_seq:
                        try:
                            seq = int(raw_seq)
                        except ValueError:
                            raise BridgeError("seq must be an integer", kind="bad_request")
                    self._data_upload(
                        request_id,
                        seq=seq,
                        final=(query.get("final") or ["1"])[0] != "0",
                    )
                    return
                if parsed.path in ("/api/http-ports/stream", "/client/http-ports/stream"):
                    # Raw byte body: route before _read_json, which would
                    # otherwise consume the stream as a JSON payload.
                    self._require_auth()
                    query = urllib.parse.parse_qs(parsed.query)
                    session_id = (query.get("id") or [""])[0]
                    direction = (query.get("direction") or [""])[0]
                    expected = "up" if parsed.path.startswith("/api/") else "down"
                    if direction != expected:
                        raise BridgeError(f"direction must be {expected!r} for {parsed.path}", kind="bad_request")
                    self._http_tunnel_body(session_id, direction)
                    return
                parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/")]
                if len(parts) == 5 and parts[:2] == ["api", "remotes"] and parts[3] == "files" and parts[4] == "write-stream":
                    self._require_auth()
                    query = urllib.parse.parse_qs(parsed.query)
                    remote = parts[2]
                    path = (query.get("path") or [""])[0]
                    self._write_stream(remote, path)
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
                if parsed.path == "/api/http-ports/open":
                    self._require_auth()
                    timeout = float(payload.pop("timeout", HTTP_TUNNEL_OPEN_TIMEOUT) or HTTP_TUNNEL_OPEN_TIMEOUT)
                    self._json(HTTPStatus.OK, state.create_http_port_tunnel(payload, timeout))
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
                    if action == "read-stream":
                        self._read_stream(remote, payload)
                        return
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

        def _data_upload(self, request_id: str, *, seq: int | None = None, final: bool = True) -> None:
            """Relay the remote's raw /client/data body into the waiting CLI stream.

            Reads the request body in 1 MiB chunks and pushes each onto the
            stream queue; ``None`` marks EOF, a ``dict`` marks a mid-stream
            failure. Runs in its own handler thread (ThreadingHTTPServer), so
            bulk data never stalls the control SSE handler for this remote.

            A slicing client sends a file as consecutive bodies sharing one
            stream: ``seq`` numbers them and only the final slice puts EOF on
            the queue. ``seq=None`` is the single-shot form older clients send,
            which still ends the stream in one go.
            """
            stream = state.get_stream(request_id)
            if stream is None:
                raise BridgeError(f"unknown data stream {request_id!r}", kind="not_found")
            if seq is not None and not state.claim_read_slice(request_id, seq):
                # The bytes would land in the wrong place, so refuse rather
                # than corrupt the file. The body goes unread, which would
                # desync the next request on a kept-alive connection, so close.
                self.close_connection = True
                raise BridgeError(
                    f"data slice {seq} is out of order for stream {request_id!r}",
                    kind="bad_request",
                )
            q = stream[0]
            dead = False
            try:
                for chunk in self._data_body_chunks():
                    q.put(chunk)
                if final:
                    q.put(None)
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                dead = True
                q.put({"error": {"kind": "remote_disconnected", "message": str(exc), "code": 2}})
            if final or dead:
                state.close_stream(request_id)
            self._json(HTTPStatus.OK, {"ok": True})

        def _data_body_chunks(self) -> Iterator[bytes]:
            """Yield the upload body, honouring whichever framing it arrives in.

            A proxy between the remote and the broker may drop Content-Length
            and let Node re-frame the body as chunked. Reading those bytes raw
            mixes the framing into the payload *and* never reaches a clean end,
            so the stream stalls until the idle timeout and arrives truncated.
            """
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                yield from _iter_chunked(self.rfile)
                return
            # A declared length bounds the read; no declaration means no body,
            # which is what a bodyless POST looks like. Reading to EOF instead
            # would hang: the connection is kept alive and has no EOF to wait
            # for, so the upload would sit there until the idle timeout.
            remaining = int(self.headers.get("Content-Length") or 0)
            while remaining > 0:
                chunk = self.rfile.read(min(STREAM_CHUNK_SIZE, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk

        def _read_stream(self, remote: str, payload: dict[str, Any]) -> None:
            """Stream a file read back to the CLI as application/octet-stream.

            Fire-and-forget on the control SSE; waits for the remote's
            independent data connection, then relays its chunks verbatim using
            chunked transfer-encoding (size is not known up front).
            """
            path = str(payload.get("path") or "")
            if not path:
                raise BridgeError("path is required", kind="bad_request")
            request_id, q = state.open_read_stream(remote, path)
            try:
                item = q.get(timeout=STREAM_OPEN_TIMEOUT)
            except queue.Empty:
                state.close_stream(request_id)
                raise BridgeError(
                    f"bridge remote {remote!r} did not open its data stream in time",
                    kind="timeout",
                    exit_code=124,
                )
            if isinstance(item, dict):
                state.close_stream(request_id)
                error = item.get("error") if isinstance(item.get("error"), dict) else {}
                raise BridgeError(
                    str(error.get("message") or "remote failed to open the data stream"),
                    kind=str(error.get("kind") or "client_error"),
                    exit_code=int(error.get("code") or 1),
                )
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._aimux_response_started = True
            try:
                while item is not None:
                    if isinstance(item, dict):
                        break  # mid-stream failure: end the chunked body early
                    data = bytes(item)
                    if data:
                        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
                    try:
                        item = q.get(timeout=STREAM_IDLE_TIMEOUT)
                    except queue.Empty:
                        break
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                state.close_stream(request_id)

        def _data_download(self, request_id: str) -> None:
            """Relay chunks queued by the CLI's write-stream request into the
            remote's ``GET /client/data`` response. Signals readiness first, so
            the write-stream handler only starts reading the CLI body once the
            remote is actually connected (no eager buffering)."""
            stream = state.get_stream(request_id)
            if stream is None:
                raise BridgeError(f"unknown data stream {request_id!r}", kind="not_found")
            q, ready, _result_q = stream
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._aimux_response_started = True
            if ready is not None:
                ready.set()
            try:
                while True:
                    item = q.get(timeout=STREAM_IDLE_TIMEOUT)
                    if item is None:
                        break
                    if isinstance(item, dict):
                        break  # error: end the chunked body early
                    data = bytes(item)
                    if data:
                        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except queue.Empty:
                pass
            except (BrokenPipeError, ConnectionError, OSError):
                pass

        def _write_stream(self, remote: str, path: str) -> None:
            """Stream a CLI file upload through to the remote's data connection.

            Waits for the remote to open its ``GET /client/data``, relays the
            CLI request body chunk-by-chunk, then waits for the remote's write
            result before answering the CLI.
            """
            if not path:
                raise BridgeError("path is required", kind="bad_request")
            length = int(self.headers.get("Content-Length") or 0)
            request_id, q, ready, result_q = state.open_write_stream(remote, path, length)
            if not ready.wait(timeout=STREAM_OPEN_TIMEOUT):
                state.close_stream(request_id)
                raise BridgeError(
                    f"bridge remote {remote!r} did not open its data stream in time",
                    kind="timeout",
                    exit_code=124,
                )
            remaining = length
            try:
                while remaining > 0:
                    chunk = self.rfile.read(min(STREAM_CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    q.put(chunk)
                q.put(None)
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                q.put({"error": {"kind": "remote_disconnected", "message": str(exc), "code": 2}})
            try:
                result = result_q.get(timeout=STREAM_IDLE_TIMEOUT)
            except queue.Empty:
                state.close_stream(request_id)
                raise BridgeError(f"bridge remote {remote!r} did not report write result in time", kind="timeout", exit_code=124)
            finally:
                state.close_stream(request_id)
            if isinstance(result, dict):
                if not result.get("ok"):
                    error = result.get("error") if isinstance(result.get("error"), dict) else {}
                    raise BridgeError(
                        str(error.get("message") or "remote write failed"),
                        kind=str(error.get("kind") or "client_error"),
                        exit_code=int(error.get("code") or 1),
                    )
            self._json(HTTPStatus.OK, {"ok": True, "size": length})

        # -- client-to-client http port tunnels -------------------------------

        def _http_tunnel_sse(self, session_id: str, direction: str) -> None:
            """Relay one tunnel direction to a client as SSE frames.

            SSE is a text protocol, so binary payloads ride base64-encoded. The
            stream ends with an explicit ``{"eof": true}`` frame, which the peer
            turns into a TCP half-close on the socket it is holding.
            """
            tunnel = state.get_http_tunnel(session_id)
            if tunnel is None:
                raise BridgeError(f"unknown http port tunnel {session_id!r}", kind="not_found")
            pipe = tunnel.pipe(direction)
            with state.lock:
                tunnel.streams_open += 1
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._aimux_response_started = True
            saw_eof = False
            last_keepalive = time.monotonic()
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while True:
                    try:
                        chunk = pipe.chunks.get(timeout=TUNNEL_SSE_POLL)
                    except queue.Empty:
                        if pipe.producer_done.is_set():
                            self._write_tunnel_frame({"eof": True})
                            saw_eof = True
                            break
                        now = time.monotonic()
                        if pipe.abandoned.is_set():
                            break
                        if now - tunnel.last_activity > HTTP_TUNNEL_IDLE_TIMEOUT:
                            # Idle is session-wide, not per-direction: a stream
                            # that only ever flows one way must not be killed
                            # for the other half's silence.
                            self._write_tunnel_frame(
                                {
                                    "error": {
                                        "kind": "timeout",
                                        "message": "tunnel idle for too long",
                                        "code": 124,
                                    }
                                }
                            )
                            break
                        if now - last_keepalive >= TUNNEL_SSE_KEEPALIVE:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            last_keepalive = now
                        continue
                    self._write_tunnel_frame(
                        {"b64": base64.b64encode(chunk).decode("ascii")}
                    )
                    last_keepalive = time.monotonic()
                    state.note_tunnel_activity(session_id)
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                with state.lock:
                    tunnel.streams_open = max(0, tunnel.streams_open - 1)
                # The body has no length and is not chunked, so closing is the
                # only honest way to end it. Skipping this leaves the peer's
                # reader blocked on a socket that never closes.
                self.close_connection = True
                if not saw_eof:
                    # The reader went away without the peer's EOF, so nobody is
                    # left to drain this pipe. End the session rather than let
                    # the producer block on a full queue forever.
                    state.close_http_tunnel(session_id, reason=f"{direction} downlink ended")

        def _write_tunnel_frame(self, payload: dict[str, Any]) -> None:
            frame = "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()

        def _iter_body_chunks(self) -> Iterator[bytes]:
            """Yield this request's body as it arrives, in either framing style.

            The aimux halves always send chunked (the length is unknown up
            front), but Content-Length is accepted so the endpoint stays usable
            from curl and from tests.
            """
            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                yield from _iter_chunked(self.rfile)
                return
            remaining = int(self.headers.get("Content-Length") or 0)
            while remaining > 0:
                chunk = self.rfile.read(min(HTTP_TUNNEL_CHUNK_SIZE, remaining))
                if not chunk:
                    return
                remaining -= len(chunk)
                yield chunk

        def _http_tunnel_body(self, session_id: str, direction: str) -> None:
            """Relay a client's raw request body into a tunnel queue.

            Each chunk goes straight onto a bounded queue, so a slow reader
            stops this read and applies TCP backpressure to the producer.
            """
            tunnel = state.get_http_tunnel(session_id)
            if tunnel is None:
                raise BridgeError(f"unknown http port tunnel {session_id!r}", kind="not_found")
            pipe = tunnel.pipe(direction)
            with state.lock:
                tunnel.streams_open += 1
            abandoned = False
            try:
                for chunk in self._iter_body_chunks():
                    if not _put_with_backpressure(pipe, chunk):
                        abandoned = True
                        break
                    state.note_tunnel_activity(session_id)
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                with state.lock:
                    tunnel.streams_open = max(0, tunnel.streams_open - 1)
                # Always signal the reader, even on an abrupt disconnect — from
                # its side the byte stream simply ended, which is the truth.
                state.finish_tunnel_direction(session_id, direction)
            try:
                self._json(HTTPStatus.OK, {"ok": True, "abandoned": abandoned})
            except (BrokenPipeError, ConnectionError, OSError):
                # A browser closing its tab is the normal way this ends, so the
                # caller is often gone by the time we answer. Nothing to report.
                self.close_connection = True

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
            elif e.kind == "port_not_allowed":
                status = HTTPStatus.FORBIDDEN
            try:
                self._json(status, {"error": {"kind": e.kind, "message": e.message, "code": e.exit_code}})
            except (BrokenPipeError, ConnectionError, OSError):
                # The caller hung up before we could answer — e.g. it timed out
                # while this request waited on a slow remote. The error has
                # nowhere to go, so close quietly instead of raising out of the
                # handler thread.
                self.close_connection = True

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
