"""Byte-transparent client-to-client HTTP port tunnel over an aimux bridge.

The tunnel moves raw bytes, so it carries ordinary HTTP, streaming SSE
responses, and WebSocket upgrades alike — nothing here parses HTTP. The two
halves are symmetric:

* the *reroute* side (``aimux local-http-port-reroute``) listens on a local
  TCP port and asks the broker to reach another client's port;
* the *serving* side is an ordinary ``aimux reverse connect`` client that
  dials ``127.0.0.1:<port>`` and relays, gated by its ``--allow-http-ports``
  allowlist.

Per session each side holds two HTTP connections to the broker:

* downlink — a new SSE stream carrying base64 frames (SSE is a text protocol,
  so arbitrary bytes must be encoded);
* uplink — a chunked POST body carrying raw bytes (a request body is already
  binary, so base64 there would be pure overhead).

Either direction ending is a half-close: the POST body finishing means the
sender saw EOF, and the broker ending the SSE stream means the peer did.
"""
from __future__ import annotations

import base64
import http.client
import json
import socket
import socketserver
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterator

from loguru import logger

from .bridge import BridgeError


# Latency beats throughput for a port tunnel (interactive HTTP, dev servers),
# so chunks stay well below the 1 MiB used by file transfer.
TUNNEL_CHUNK_SIZE = 64 * 1024
# Bound the in-flight bytes per direction. A full queue blocks the relaying
# handler, which stops reading its source socket and pushes backpressure all
# the way to the original producer. Without this a fast producer and a slow
# consumer grow the broker's heap without limit.
TUNNEL_QUEUE_DEPTH = 32
# Must exceed the broker's own handshake wait, otherwise the caller gives up
# and closes the socket while the broker is still waiting for the far side —
# the broker then has nobody left to report the real error to.
TUNNEL_OPEN_TIMEOUT = 25.0
TUNNEL_DIAL_TIMEOUT = 10.0
# The broker emits a comment frame on an idle tunnel stream well inside this,
# so a timeout here means the link is genuinely wedged rather than quiet.
TUNNEL_READ_TIMEOUT = 120.0
# Idle keep-alive HTTP connections are normal, so this is deliberately loose —
# it exists to reclaim abandoned sessions, not to police idle ones.
TUNNEL_IDLE_TIMEOUT = 300.0
_MAX_CHUNK_LINE = 16 * 1024
_MAX_HEADER_LINE = 64 * 1024


def parse_port_list(value: str | None) -> tuple[int, ...]:
    """Parse a ``--allow-http-ports`` value such as ``8000,8001``.

    Raises ``BridgeError`` on anything malformed: a typo in an allowlist must
    be loud, since silently dropping an entry would look like a client that
    simply refuses to be reached.
    """
    if not value:
        return ()
    ports: list[int] = []
    for item in str(value).split(","):
        text = item.strip()
        if not text:
            continue
        try:
            port = int(text)
        except ValueError as exc:
            raise BridgeError(f"invalid port {text!r} in --allow-http-ports", kind="bad_argument") from exc
        if not 1 <= port <= 65535:
            raise BridgeError(f"port {port} in --allow-http-ports is out of range 1-65535", kind="bad_argument")
        if port not in ports:
            ports.append(port)
    return tuple(ports)


def split_base_url(base: str) -> tuple[str, int, str, str]:
    """Split a bridge URL into ``(scheme, host, port, base_path)``."""
    parsed = urllib.parse.urlsplit(base.rstrip("/"))
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)
    return scheme, host, port, parsed.path.rstrip("/")


def open_bridge_connection(base: str, *, timeout: float | None = None) -> http.client.HTTPConnection:
    scheme, host, port, _ = split_base_url(base)
    if scheme == "https":
        return http.client.HTTPSConnection(host, port, timeout=timeout)
    return http.client.HTTPConnection(host, port, timeout=timeout)


def bridge_path(base: str, path: str) -> str:
    return f"{split_base_url(base)[3]}{path}"


def iter_sse_payloads(response: Any) -> Iterator[dict[str, Any]]:
    """Yield the JSON object carried by each SSE ``data:`` frame."""
    data_lines: list[str] = []
    for raw in response:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if data_lines:
                joined = "\n".join(data_lines)
                data_lines = []
                try:
                    payload = json.loads(joined)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield payload
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())


def iter_tunnel_chunks(response: Any) -> Iterator[bytes]:
    """Yield byte chunks from a tunnel downlink until the peer's EOF frame."""
    for payload in iter_sse_payloads(response):
        error = payload.get("error")
        if isinstance(error, dict):
            raise BridgeError(
                str(error.get("message") or "tunnel stream failed"),
                kind=str(error.get("kind") or "client_error"),
                exit_code=int(error.get("code") or 1),
            )
        if payload.get("eof"):
            return
        encoded = payload.get("b64")
        if isinstance(encoded, str) and encoded:
            yield base64.b64decode(encoded)


class ChunkedPost:
    """A streaming HTTP request body of unknown length.

    ``http.client`` only sends bodies it can measure up front, so the chunked
    framing is written by hand — the same approach the broker already uses for
    its own chunked responses.
    """

    def __init__(self, conn: http.client.HTTPConnection, path: str, token: str) -> None:
        self._conn = conn
        self._closed = False
        conn.putrequest("POST", path)
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()

    def send(self, data: bytes) -> None:
        if self._closed or not data:
            return
        self._conn.send(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")

    def close(self) -> None:
        """Terminate the body (``0\\r\\n\\r\\n``) and drain the response."""
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.send(b"0\r\n\r\n")
            self._conn.getresponse().read()
        except Exception:  # pragma: no cover - the peer may already be gone
            pass


def _shutdown_write(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_WR)
    except OSError:
        pass


def _force_shutdown(sock: socket.socket) -> None:
    """Wake a peer thread blocked in ``recv`` on the same socket.

    Shutdown only, never ``close``: closing here would release the descriptor
    while the other pump may still be blocked on it, letting an unrelated
    ``accept`` reuse the number and hand that thread somebody else's socket.
    The owning thread closes it once both pumps have finished.
    """
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def relay_socket_over_bridge(
    *,
    base: str,
    token: str,
    session_id: str,
    sock: socket.socket,
    prefix: str,
) -> None:
    """Pump bytes both ways between ``sock`` and a broker tunnel session.

    ``prefix`` selects the endpoint family: ``/api`` for the reroute side,
    ``/client`` for the reverse client that owns the target port. Returns once
    both directions have finished (or either one fails).
    """
    query = urllib.parse.urlencode({"id": session_id})
    # The broker names directions by what the payload *is*, not by who asks:
    # "up" is what the browser sent, "down" is what the target answered. The
    # two halves therefore read opposite directions from their own socket.
    if prefix == "/client":
        read_direction, write_direction = "up", "down"
    else:
        read_direction, write_direction = "down", "up"
    down_path = bridge_path(base, f"{prefix}/http-ports/stream?{query}&direction={read_direction}")
    up_path = bridge_path(base, f"{prefix}/http-ports/stream?{query}&direction={write_direction}")
    errors: list[BaseException] = []

    def down() -> None:
        conn: http.client.HTTPConnection | None = None
        try:
            # Bounded rather than infinite: if the broker wedges, the relay
            # must unwind instead of holding a thread and a socket forever.
            conn = open_bridge_connection(base, timeout=TUNNEL_READ_TIMEOUT)
            conn.putrequest("GET", down_path)
            conn.putheader("Authorization", f"Bearer {token}")
            conn.putheader("Accept", "text/event-stream")
            conn.endheaders()
            response = conn.getresponse()
            if response.status != 200:
                body = response.read().decode("utf-8", errors="replace")
                raise BridgeError(f"tunnel downlink error {response.status}: {body}", kind="remote_disconnected")
            for chunk in iter_tunnel_chunks(response):
                sock.sendall(chunk)
            _shutdown_write(sock)
        except BaseException as exc:  # noqa: BLE001 - relay must never die silently
            errors.append(exc)
            _force_shutdown(sock)
        finally:
            if conn is not None:
                conn.close()

    def up() -> None:
        conn: http.client.HTTPConnection | None = None
        post: ChunkedPost | None = None
        try:
            conn = open_bridge_connection(base, timeout=None)
            post = ChunkedPost(conn, up_path, token)
            while True:
                data = sock.recv(TUNNEL_CHUNK_SIZE)
                if not data:
                    break
                post.send(data)
        except BaseException as exc:  # noqa: BLE001 - relay must never die silently
            errors.append(exc)
            _force_shutdown(sock)
        finally:
            if post is not None:
                post.close()
            if conn is not None:
                conn.close()

    threads = [threading.Thread(target=down, daemon=True), threading.Thread(target=up, daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for exc in errors:
        if isinstance(exc, BridgeError):
            logger.warning("tunnel relay failed session={} kind={} message={}", session_id, exc.kind, exc.message)
        else:
            logger.warning("tunnel relay failed session={} error={}", session_id, exc)


# ---- reroute side: local TCP listener ---------------------------------------


@dataclass(frozen=True)
class RerouteConfig:
    base_url: str
    token: str
    remote_client: str
    remote_port: int
    open_timeout: float = TUNNEL_OPEN_TIMEOUT


def open_tunnel_session(config: RerouteConfig, *, timeout: float | None = None) -> dict[str, Any]:
    """Ask the broker to reach ``remote_client``'s ``remote_port``.

    The broker validates the target's ``--allow-http-ports`` allowlist before
    waking the far side, so an unauthorized port fails here rather than
    silently hanging.
    """
    payload = {"remote_client": config.remote_client, "port": config.remote_port}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    conn = open_bridge_connection(config.base_url, timeout=timeout or config.open_timeout)
    try:
        conn.request(
            "POST",
            bridge_path(config.base_url, "/api/http-ports/open"),
            body=data,
            headers={
                "Authorization": f"Bearer {config.token}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            # An intermediary answering with an HTML error page is not a
            # broker response; report it as unreachable rather than crashing
            # out of the connection handler.
            raise BridgeError(
                f"bridge broker returned a non-JSON reply ({response.status}): {raw[:200]}",
                kind="remote_disconnected",
                exit_code=2,
            )
        if response.status != 200:
            error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
            raise BridgeError(
                str(error.get("message") or f"broker refused the tunnel ({response.status})"),
                kind=str(error.get("kind") or "client_error"),
                exit_code=int(error.get("code") or 1),
            )
        session = parsed.get("session") if isinstance(parsed.get("session"), dict) else {}
        if not session.get("id"):
            raise BridgeError("broker returned no tunnel session", kind="client_error")
        return session
    except (OSError, http.client.HTTPException) as exc:
        raise BridgeError(f"bridge broker is unreachable: {exc}", kind="remote_disconnected", exit_code=2) from exc
    finally:
        conn.close()


def verify_remote_allows(config: RerouteConfig, *, timeout: float | None = None) -> None:
    """Fail fast when the target client cannot serve this port.

    The per-connection refusal stays authoritative, since the target's
    allowlist can change while we run. But a typo would otherwise only show up
    as a browser request dying with no reply at all, which is miserable to
    debug — so misconfiguration is reported at startup instead.

    Connectivity problems are *not* fatal: a broker that is briefly down
    should not stop the listener from coming up and serving later.
    """
    conn = open_bridge_connection(config.base_url, timeout=timeout or config.open_timeout)
    try:
        conn.request(
            "GET",
            bridge_path(config.base_url, "/api/remotes"),
            headers={"Authorization": f"Bearer {config.token}"},
        )
        response = conn.getresponse()
        raw = response.read().decode("utf-8", errors="replace")
        if response.status != 200:
            logger.warning("http port reroute could not pre-check remote: HTTP {} {}", response.status, raw[:200])
            return
        remotes = (json.loads(raw) if raw.strip() else {}).get("remotes") or []
        remote = next((item for item in remotes if item.get("name") == config.remote_client), None)
        if remote is None:
            raise BridgeError(f"bridge remote {config.remote_client!r} not found", kind="not_found")
        allowed = [int(p) for p in (remote.get("allow_http_ports") or [])]
        if config.remote_port not in allowed:
            detail = ", ".join(str(p) for p in allowed) if allowed else "it declared no --allow-http-ports"
            raise BridgeError(
                f"remote {config.remote_client!r} does not allow connections to port "
                f"{config.remote_port} ({detail})",
                kind="port_not_allowed",
            )
    except (OSError, http.client.HTTPException, ValueError, json.JSONDecodeError) as exc:
        logger.warning("http port reroute could not pre-check remote (continuing): {}", exc)
    finally:
        conn.close()


class _RerouteServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config: RerouteConfig):
        self.config = config
        super().__init__(address, _RerouteHandler)


class _RerouteHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        config: RerouteConfig = self.server.config  # type: ignore[attr-defined]
        sock: socket.socket = self.request
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        try:
            session = open_tunnel_session(config)
        except BridgeError as exc:
            # Refuse the local connection rather than proxying into a dead end.
            logger.warning(
                "http port reroute refused peer={} remote={}:{} kind={} message={}",
                peer,
                config.remote_client,
                config.remote_port,
                exc.kind,
                exc.message,
            )
            _force_shutdown(sock)
            return

        session_id = str(session["id"])
        logger.info(
            "http port reroute opened peer={} session={} remote={}:{}",
            peer,
            session_id,
            config.remote_client,
            config.remote_port,
        )
        try:
            relay_socket_over_bridge(
                base=config.base_url,
                token=config.token,
                session_id=session_id,
                sock=sock,
                prefix="/api",
            )
        finally:
            _force_shutdown(sock)
            logger.info("http port reroute closed peer={} session={}", peer, session_id)


def serve_local_http_reroute(
    *,
    local_host: str,
    local_port: int,
    base_url: str,
    token: str,
    remote_client: str,
    remote_port: int,
) -> None:
    """Listen on ``local_host:local_port`` and tunnel each connection onward."""
    config = RerouteConfig(
        base_url=base_url,
        token=token,
        remote_client=remote_client,
        remote_port=remote_port,
    )
    verify_remote_allows(config)
    server = _RerouteServer((local_host, local_port), config)
    actual_port = int(server.server_address[1])
    logger.info(
        "aimux local http port reroute listening on {}:{} -> {}:{} via {}",
        local_host,
        actual_port,
        remote_client,
        remote_port,
        base_url,
    )
    try:
        server.serve_forever()
    finally:
        logger.info("aimux local http port reroute stopped on {}:{}", local_host, actual_port)
        server.server_close()
