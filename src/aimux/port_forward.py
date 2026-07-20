"""Transparent HTTP, SSE, and WebSocket forwarding through an aimux bridge."""
from __future__ import annotations

import http.client
import re
import socket
import ssl
import threading
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterable

from loguru import logger

from .bridge import BridgeError


FORWARD_PATH = "/api/forward"
TARGET_ORIGIN_HEADER = "X-Aimux-Target-Origin"
REQUEST_TARGET_HEADER = "X-Aimux-Request-Target"
LOCAL_ORIGIN_HEADER = "X-Aimux-Local-Origin"
UPSTREAM_AUTH_HEADER = "X-Aimux-Upstream-Authorization"
UPSTREAM_HOST_HEADER = "X-Aimux-Upstream-Host"
PRESERVE_HOST_HEADER = "X-Aimux-Preserve-Host"
REWRITE_ORIGIN_HEADER = "X-Aimux-Rewrite-Origin"
REWRITE_SECURE_COOKIE_HEADER = "X-Aimux-Rewrite-Secure-Cookie"
MAX_REQUEST_TARGET_LENGTH = 16 * 1024
COPY_CHUNK_SIZE = 64 * 1024

_INTERNAL_HEADERS = {
    TARGET_ORIGIN_HEADER.lower(),
    REQUEST_TARGET_HEADER.lower(),
    LOCAL_ORIGIN_HEADER.lower(),
    UPSTREAM_AUTH_HEADER.lower(),
    UPSTREAM_HOST_HEADER.lower(),
    PRESERVE_HOST_HEADER.lower(),
    REWRITE_ORIGIN_HEADER.lower(),
    REWRITE_SECURE_COOKIE_HEADER.lower(),
}
_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-connection",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
_NO_BODY_STATUSES = {HTTPStatus.NO_CONTENT, HTTPStatus.NOT_MODIFIED}
_DOMAIN_RE = re.compile(r";\s*Domain=([^;]+)", re.IGNORECASE)
_SECURE_RE = re.compile(r";\s*Secure(?=;|$)", re.IGNORECASE)


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int

    @property
    def authority(self) -> str:
        default_port = 443 if self.scheme == "https" else 80
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return host if self.port == default_port else f"{host}:{self.port}"

    @property
    def value(self) -> str:
        return f"{self.scheme}://{self.authority}"


@dataclass(frozen=True)
class LocalForwardConfig:
    bridge_origin: Origin
    bridge_path: str
    target_origin: Origin
    token: str
    local_origin: str
    preserve_host: bool = False
    rewrite_origin: bool = True
    rewrite_secure_cookie: bool = False
    connect_timeout: float = 15.0


def parse_origin(value: str, *, label: str) -> Origin:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise BridgeError(f"{label} must use http:// or https://", kind="bad_argument")
    if not parsed.hostname:
        raise BridgeError(f"{label} must include a host", kind="bad_argument")
    if parsed.username or parsed.password:
        raise BridgeError(f"{label} must not include user information", kind="bad_argument")
    if parsed.query or parsed.fragment:
        raise BridgeError(f"{label} must not include query or fragment", kind="bad_argument")
    if parsed.path not in {"", "/"}:
        raise BridgeError(f"{label} must be an origin without a path", kind="bad_argument")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise BridgeError(f"{label} has an invalid port: {exc}", kind="bad_argument") from exc
    return Origin(parsed.scheme, parsed.hostname, port)


def parse_bridge_url(value: str) -> tuple[Origin, str]:
    parsed = urllib.parse.urlsplit(value.rstrip("/"))
    origin = parse_origin(
        urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")),
        label="bridge URL",
    )
    base_path = parsed.path.rstrip("/")
    return origin, f"{base_path}{FORWARD_PATH}"


def validate_request_target(value: str) -> str:
    if not value or not value.startswith("/"):
        raise BridgeError("X-Aimux-Request-Target must use origin-form and start with '/'", kind="bad_request")
    if len(value) > MAX_REQUEST_TARGET_LENGTH:
        raise BridgeError("X-Aimux-Request-Target is too long", kind="bad_request")
    if "\r" in value or "\n" in value or "#" in value:
        raise BridgeError("X-Aimux-Request-Target contains invalid characters", kind="bad_request")
    return value


def _connection_tokens(headers) -> set[str]:
    value = headers.get("Connection", "")
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _is_websocket_upgrade(headers) -> bool:
    return headers.get("Upgrade", "").lower() == "websocket" and "upgrade" in _connection_tokens(headers)


def _truthy_header(headers, name: str, *, default: bool = False) -> bool:
    value = headers.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _header_items(headers) -> list[tuple[str, str]]:
    return [(str(name), str(value)) for name, value in headers.items()]


def _filtered_headers(
    headers,
    *,
    strip_internal: bool,
    strip_authorization: bool,
    strip_host: bool,
) -> list[tuple[str, str]]:
    connection_tokens = _connection_tokens(headers)
    blocked = set(_HOP_BY_HOP_HEADERS) | connection_tokens
    if strip_internal:
        blocked |= _INTERNAL_HEADERS
    if strip_authorization:
        blocked.add("authorization")
    if strip_host:
        blocked.add("host")
    return [(name, value) for name, value in _header_items(headers) if name.lower() not in blocked]


def _body_mode(headers) -> tuple[int | None, bool]:
    raw_length = headers.get("Content-Length")
    if raw_length is not None:
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise BridgeError("invalid Content-Length", kind="bad_request") from exc
        if length < 0:
            raise BridgeError("invalid Content-Length", kind="bad_request")
        return length, False
    chunked = "chunked" in headers.get("Transfer-Encoding", "").lower()
    return None, chunked


def _copy_request_body(handler: BaseHTTPRequestHandler, connection: http.client.HTTPConnection) -> None:
    length, chunked = _body_mode(handler.headers)
    if length is not None:
        remaining = length
        while remaining:
            chunk = handler.rfile.read(min(COPY_CHUNK_SIZE, remaining))
            if not chunk:
                raise BridgeError("request body ended before Content-Length", kind="bad_request")
            connection.send(chunk)
            remaining -= len(chunk)
        return
    if not chunked:
        return
    while True:
        line = handler.rfile.readline(MAX_REQUEST_TARGET_LENGTH)
        if not line:
            raise BridgeError("chunked request body ended unexpectedly", kind="bad_request")
        connection.send(line)
        size_text = line.split(b";", 1)[0].strip()
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise BridgeError("invalid chunked request body", kind="bad_request") from exc
        if size == 0:
            while True:
                trailer = handler.rfile.readline(MAX_REQUEST_TARGET_LENGTH)
                if not trailer:
                    raise BridgeError("chunked request trailers ended unexpectedly", kind="bad_request")
                connection.send(trailer)
                if trailer in {b"\r\n", b"\n"}:
                    return
        payload = handler.rfile.read(size + 2)
        if len(payload) != size + 2:
            raise BridgeError("chunked request body ended unexpectedly", kind="bad_request")
        connection.send(payload)


def _open_http_connection(origin: Origin, timeout: float | None) -> http.client.HTTPConnection:
    if origin.scheme == "https":
        return http.client.HTTPSConnection(origin.host, origin.port, timeout=timeout)
    return http.client.HTTPConnection(origin.host, origin.port, timeout=timeout)


def _send_streaming_request(
    handler: BaseHTTPRequestHandler,
    *,
    origin: Origin,
    request_target: str,
    headers: Iterable[tuple[str, str]],
    timeout: float | None,
) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
    connection = _open_http_connection(origin, timeout)
    length, chunked = _body_mode(handler.headers)
    connection.putrequest(handler.command, request_target, skip_host=True, skip_accept_encoding=True)
    for name, value in headers:
        if name.lower() in {"content-length", "transfer-encoding"}:
            continue
        connection.putheader(name, value)
    if length is not None:
        connection.putheader("Content-Length", str(length))
    elif chunked:
        connection.putheader("Transfer-Encoding", "chunked")
    connection.endheaders()
    _copy_request_body(handler, connection)
    return connection, connection.getresponse()


def _rewrite_location(value: str, target_origin: Origin, local_origin: str) -> str:
    if local_origin and value.startswith(target_origin.value):
        return local_origin.rstrip("/") + value[len(target_origin.value):]
    return value


def _rewrite_cookie(value: str, target_origin: Origin, rewrite_secure: bool) -> str:
    def replace_domain(match: re.Match[str]) -> str:
        domain = match.group(1).strip().lstrip(".")
        return "" if domain.lower() == target_origin.host.lower().lstrip(".") else match.group(0)

    rewritten = _DOMAIN_RE.sub(replace_domain, value)
    if rewrite_secure:
        rewritten = _SECURE_RE.sub("", rewritten)
    return rewritten


def _response_has_body(method: str, status: int) -> bool:
    return method != "HEAD" and status not in _NO_BODY_STATUSES and not 100 <= status < 200


def _copy_response(
    handler: BaseHTTPRequestHandler,
    response: http.client.HTTPResponse,
    *,
    target_origin: Origin | None = None,
    local_origin: str = "",
    rewrite_secure_cookie: bool = False,
) -> None:
    setattr(handler, "_aimux_response_started", True)
    handler.send_response(response.status, response.reason)
    content_length: str | None = None
    content_type = ""
    sent_headers: set[str] = set()
    for name, value in response.getheaders():
        lowered = name.lower()
        if lowered in _HOP_BY_HOP_HEADERS or lowered == "server" or lowered == "date":
            continue
        if lowered == "content-length":
            content_length = value
        if lowered == "content-type":
            content_type = value.lower()
        if target_origin is not None and lowered == "location":
            value = _rewrite_location(value, target_origin, local_origin)
        elif target_origin is not None and lowered == "set-cookie":
            value = _rewrite_cookie(value, target_origin, rewrite_secure_cookie)
        elif target_origin is not None and lowered == "access-control-allow-origin" and value == target_origin.value:
            value = local_origin or value
        handler.send_header(name, value)
        sent_headers.add(lowered)
    if "text/event-stream" in content_type:
        if "cache-control" not in sent_headers:
            handler.send_header("Cache-Control", "no-cache, no-transform")
        if "x-accel-buffering" not in sent_headers:
            handler.send_header("X-Accel-Buffering", "no")
    if content_length is None and _response_has_body(handler.command, response.status):
        handler.send_header("Connection", "close")
        handler.close_connection = True
    handler.end_headers()
    if not _response_has_body(handler.command, response.status):
        return
    read_chunk = response.read1 if "text/event-stream" in content_type else response.read
    while True:
        chunk = read_chunk(COPY_CHUNK_SIZE)
        if not chunk:
            return
        handler.wfile.write(chunk)
        if "text/event-stream" in content_type:
            handler.wfile.flush()


def _replace_origin(value: str, local_origin: str, target_origin: Origin) -> str:
    if local_origin and value.startswith(local_origin):
        return target_origin.value + value[len(local_origin):]
    return value


def _bridge_upstream_headers(handler: BaseHTTPRequestHandler, target_origin: Origin) -> list[tuple[str, str]]:
    preserve_host = _truthy_header(handler.headers, PRESERVE_HOST_HEADER)
    rewrite_origin = _truthy_header(handler.headers, REWRITE_ORIGIN_HEADER, default=True)
    local_origin = handler.headers.get(LOCAL_ORIGIN_HEADER, "")
    upstream_auth = handler.headers.get(UPSTREAM_AUTH_HEADER)
    upstream_host = handler.headers.get(UPSTREAM_HOST_HEADER)
    headers = _filtered_headers(
        handler.headers,
        strip_internal=True,
        strip_authorization=True,
        strip_host=True,
    )
    output: list[tuple[str, str]] = []
    for name, value in headers:
        if rewrite_origin and name.lower() in {"origin", "referer"}:
            value = _replace_origin(value, local_origin, target_origin)
        output.append((name, value))
    output.append(("Host", upstream_host if preserve_host and upstream_host else target_origin.authority))
    if upstream_auth:
        output.append(("Authorization", upstream_auth))
    forwarded_for = handler.headers.get("X-Forwarded-For")
    peer = handler.client_address[0] if handler.client_address else ""
    output.append(("X-Forwarded-For", f"{forwarded_for}, {peer}" if forwarded_for else peer))
    output.append(("X-Forwarded-Host", upstream_host or ""))
    output.append(("X-Forwarded-Proto", urllib.parse.urlsplit(local_origin).scheme or "http"))
    return output


def _local_bridge_headers(handler: BaseHTTPRequestHandler, config: LocalForwardConfig) -> list[tuple[str, str]]:
    original_auth = handler.headers.get("Authorization")
    original_host = handler.headers.get("Host", "")
    headers = _filtered_headers(
        handler.headers,
        strip_internal=True,
        strip_authorization=True,
        strip_host=True,
    )
    headers.extend(
        [
            ("Host", config.bridge_origin.authority),
            ("Authorization", f"Bearer {config.token}"),
            (TARGET_ORIGIN_HEADER, config.target_origin.value),
            (REQUEST_TARGET_HEADER, validate_request_target(handler.path)),
            (LOCAL_ORIGIN_HEADER, config.local_origin),
            (UPSTREAM_HOST_HEADER, original_host),
            (PRESERVE_HOST_HEADER, "1" if config.preserve_host else "0"),
            (REWRITE_ORIGIN_HEADER, "1" if config.rewrite_origin else "0"),
            (REWRITE_SECURE_COOKIE_HEADER, "1" if config.rewrite_secure_cookie else "0"),
        ]
    )
    if original_auth:
        headers.append((UPSTREAM_AUTH_HEADER, original_auth))
    return headers


def handle_bridge_forward(handler: BaseHTTPRequestHandler) -> None:
    target_origin = parse_origin(handler.headers.get(TARGET_ORIGIN_HEADER, ""), label=TARGET_ORIGIN_HEADER)
    request_target = validate_request_target(handler.headers.get(REQUEST_TARGET_HEADER, ""))
    if _is_websocket_upgrade(handler.headers):
        _proxy_websocket(
            handler,
            origin=target_origin,
            request_target=request_target,
            headers=_bridge_upstream_headers(handler, target_origin),
        )
        return
    connection: http.client.HTTPConnection | None = None
    try:
        connection, response = _send_streaming_request(
            handler,
            origin=target_origin,
            request_target=request_target,
            headers=_bridge_upstream_headers(handler, target_origin),
            timeout=None,
        )
        _copy_response(
            handler,
            response,
            target_origin=target_origin,
            local_origin=handler.headers.get(LOCAL_ORIGIN_HEADER, ""),
            rewrite_secure_cookie=_truthy_header(handler.headers, REWRITE_SECURE_COOKIE_HEADER),
        )
    except (OSError, http.client.HTTPException) as exc:
        if getattr(handler, "_aimux_response_started", False):
            handler.close_connection = True
            return
        raise BridgeError(f"forward target is unreachable: {exc}", kind="remote_unreachable", exit_code=2) from exc
    finally:
        if connection is not None:
            connection.close()


def _open_socket(origin: Origin, timeout: float | None) -> socket.socket:
    raw = socket.create_connection((origin.host, origin.port), timeout=timeout)
    if origin.scheme == "https":
        return ssl.create_default_context().wrap_socket(raw, server_hostname=origin.host)
    return raw


def _format_upgrade_request(method: str, path: str, headers: Iterable[tuple[str, str]]) -> bytes:
    lines = [f"{method} {path} HTTP/1.1"]
    lines.extend(f"{name}: {value}" for name, value in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _read_http_head(sock: socket.socket, *, max_size: int = 64 * 1024) -> tuple[bytes, bytes]:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("connection closed during HTTP upgrade")
        data.extend(chunk)
        if len(data) > max_size:
            raise OSError("HTTP upgrade headers are too large")
    head, rest = bytes(data).split(b"\r\n\r\n", 1)
    return head + b"\r\n\r\n", rest


def _status_from_head(head: bytes) -> int:
    first_line = head.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
    parts = first_line.split(" ", 2)
    if len(parts) < 2:
        return 0
    try:
        return int(parts[1])
    except ValueError:
        return 0


def _relay_sockets(left: socket.socket, right: socket.socket, *, initial_right_to_left: bytes = b"") -> None:
    if initial_right_to_left:
        left.sendall(initial_right_to_left)
    stopped = threading.Event()

    def copy(source: socket.socket, destination: socket.socket) -> None:
        try:
            while not stopped.is_set():
                data = source.recv(COPY_CHUNK_SIZE)
                if not data:
                    return
                destination.sendall(data)
        except OSError:
            return
        finally:
            stopped.set()
            try:
                destination.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    first = threading.Thread(target=copy, args=(left, right), daemon=True)
    second = threading.Thread(target=copy, args=(right, left), daemon=True)
    first.start()
    second.start()
    first.join()
    second.join()


def _proxy_websocket(
    handler: BaseHTTPRequestHandler,
    *,
    origin: Origin,
    request_target: str,
    headers: list[tuple[str, str]],
) -> None:
    filtered = [(name, value) for name, value in headers if name.lower() not in {"content-length", "transfer-encoding"}]
    filtered.extend([("Connection", "Upgrade"), ("Upgrade", "websocket")])
    upstream = _open_socket(origin, None)
    try:
        upstream.sendall(_format_upgrade_request(handler.command, request_target, filtered))
        response_head, response_rest = _read_http_head(upstream)
        handler.wfile.write(response_head)
        handler.wfile.flush()
        if _status_from_head(response_head) != HTTPStatus.SWITCHING_PROTOCOLS:
            if response_rest:
                handler.wfile.write(response_rest)
            handler.close_connection = True
            return
        logger.info("port forward websocket connected target={} path={}", origin.value, request_target)
        _relay_sockets(handler.connection, upstream, initial_right_to_left=response_rest)
    finally:
        upstream.close()
        handler.close_connection = True


def _make_local_handler(config: LocalForwardConfig) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
            self._forward()

        def do_PUT(self) -> None:  # noqa: N802
            self._forward()

        def do_PATCH(self) -> None:  # noqa: N802
            self._forward()

        def do_DELETE(self) -> None:  # noqa: N802
            self._forward()

        def do_HEAD(self) -> None:  # noqa: N802
            self._forward()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._forward()

        def _forward(self) -> None:
            try:
                if _is_websocket_upgrade(self.headers):
                    _proxy_websocket(
                        self,
                        origin=config.bridge_origin,
                        request_target=config.bridge_path,
                        headers=_local_bridge_headers(self, config),
                    )
                    return
                connection: http.client.HTTPConnection | None = None
                try:
                    connection, response = _send_streaming_request(
                        self,
                        origin=config.bridge_origin,
                        request_target=config.bridge_path,
                        headers=_local_bridge_headers(self, config),
                        timeout=None,
                    )
                    _copy_response(self, response)
                finally:
                    if connection is not None:
                        connection.close()
            except BridgeError as exc:
                if not getattr(self, "_aimux_response_started", False):
                    self._error(HTTPStatus.BAD_REQUEST, exc.message)
            except (OSError, http.client.HTTPException) as exc:
                if not getattr(self, "_aimux_response_started", False):
                    self._error(HTTPStatus.BAD_GATEWAY, f"bridge forward failed: {exc}")

        def _error(self, status: HTTPStatus, message: str) -> None:
            if self.wfile.closed:
                return
            data = message.encode("utf-8", errors="replace")
            self.send_response(status.value)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True

        def log_message(self, fmt: str, *args) -> None:
            return

    return Handler


def serve_local_forward(
    bridge_url: str,
    *,
    local_host: str,
    local_port: int,
    target_origin: str,
    token: str,
    preserve_host: bool = False,
    rewrite_origin: bool = True,
    rewrite_secure_cookie: bool = False,
) -> None:
    bridge_origin, bridge_path = parse_bridge_url(bridge_url)
    target = parse_origin(target_origin, label="target origin")
    server = ThreadingHTTPServer((local_host, local_port), BaseHTTPRequestHandler, bind_and_activate=False)
    server.server_bind()
    actual_port = int(server.server_address[1])
    local_origin = f"http://{local_host}:{actual_port}"
    server.RequestHandlerClass = _make_local_handler(
        LocalForwardConfig(
            bridge_origin=bridge_origin,
            bridge_path=bridge_path,
            target_origin=target,
            token=token,
            local_origin=local_origin,
            preserve_host=preserve_host,
            rewrite_origin=rewrite_origin,
            rewrite_secure_cookie=rewrite_secure_cookie,
        )
    )
    server.server_activate()
    logger.info("aimux port forward listening on {} -> {} via {}", local_origin, target.value, bridge_url.rstrip("/"))
    try:
        server.serve_forever()
    finally:
        logger.info("aimux port forward stopped local={} target={}", local_origin, target.value)
        server.server_close()
