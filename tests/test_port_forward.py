from __future__ import annotations

import http.client
import socket
import threading
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from typer.testing import CliRunner

from aimux import port_forward
from aimux.bridge import BridgeError
from aimux.bridge_server import BridgeBrokerState, make_handler
from aimux.cli import app


@contextmanager
def _running_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_parse_origin_and_request_target_validation():
    origin = port_forward.parse_origin("https://example.test:9443", label="target")
    assert origin.value == "https://example.test:9443"
    assert port_forward.validate_request_target("/a%2Fb?q=1") == "/a%2Fb?q=1"

    with pytest.raises(BridgeError):
        port_forward.parse_origin("ssh://example.test", label="target")
    with pytest.raises(BridgeError):
        port_forward.parse_origin("http://example.test/path", label="target")
    with pytest.raises(BridgeError):
        port_forward.validate_request_target("http://example.test/path")
    with pytest.raises(BridgeError):
        port_forward.validate_request_target("/path#fragment")


def test_http_forward_preserves_request_and_rewrites_response_headers():
    observed: dict[str, str] = {}

    class TargetHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            observed["path"] = self.path
            observed["host"] = self.headers.get("Host", "")
            observed["authorization"] = self.headers.get("Authorization", "")
            observed["origin"] = self.headers.get("Origin", "")
            observed["body"] = self.rfile.read(length).decode("utf-8")
            body = b"forwarded"
            self.send_response(HTTPStatus.CREATED)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/next")
            self.send_header("Set-Cookie", "sid=abc; Domain=127.0.0.1; Path=/; HttpOnly")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    with _running_server(TargetHandler) as target:
        bridge_state = BridgeBrokerState("secret")
        with _running_server(make_handler(bridge_state)) as broker:
            bridge_origin = port_forward.Origin("http", "127.0.0.1", broker.server_address[1])
            target_origin = port_forward.Origin("http", "127.0.0.1", target.server_address[1])
            config = port_forward.LocalForwardConfig(
                bridge_origin=bridge_origin,
                bridge_path=port_forward.FORWARD_PATH,
                target_origin=target_origin,
                token="secret",
                local_origin="http://127.0.0.1:18080",
            )
            with _running_server(port_forward._make_local_handler(config)) as local:
                connection = http.client.HTTPConnection("127.0.0.1", local.server_address[1], timeout=5)
                connection.request(
                    "POST",
                    "/api/a%2Fb?q=1",
                    body=b'{"ok":true}',
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer app-token",
                        "Origin": "http://127.0.0.1:18080",
                    },
                )
                response = connection.getresponse()
                body = response.read()
                headers = response.getheaders()
                connection.close()

    assert response.status == HTTPStatus.CREATED
    assert body == b"forwarded"
    assert observed == {
        "path": "/api/a%2Fb?q=1",
        "host": target_origin.authority,
        "authorization": "Bearer app-token",
        "origin": target_origin.value,
        "body": '{"ok":true}',
    }
    assert ("Location", "http://127.0.0.1:18080/next") in headers
    cookie = next(value for name, value in headers if name.lower() == "set-cookie")
    assert "Domain=" not in cookie
    assert "HttpOnly" in cookie


def test_sse_forward_streams_event_response():
    class TargetHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: one\n\n")
            self.wfile.flush()
            self.wfile.write(b"data: two\n\n")
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, fmt, *args):
            return

    with _running_server(TargetHandler) as target:
        with _running_server(make_handler(BridgeBrokerState("secret"))) as broker:
            config = port_forward.LocalForwardConfig(
                bridge_origin=port_forward.Origin("http", "127.0.0.1", broker.server_address[1]),
                bridge_path=port_forward.FORWARD_PATH,
                target_origin=port_forward.Origin("http", "127.0.0.1", target.server_address[1]),
                token="secret",
                local_origin="http://127.0.0.1:18080",
            )
            with _running_server(port_forward._make_local_handler(config)) as local:
                connection = http.client.HTTPConnection("127.0.0.1", local.server_address[1], timeout=5)
                connection.request("GET", "/events")
                response = connection.getresponse()
                body = response.read()
                connection.close()

    assert response.status == HTTPStatus.OK
    assert response.getheader("Content-Type") == "text/event-stream"
    assert response.getheader("X-Accel-Buffering") == "no"
    assert body == b"data: one\n\ndata: two\n\n"


def test_websocket_upgrade_uses_same_forward_endpoint():
    observed: dict[str, str] = {}

    class TargetHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):  # noqa: N802
            observed["path"] = self.path
            observed["upgrade"] = self.headers.get("Upgrade", "")
            self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
            self.send_header("Connection", "Upgrade")
            self.send_header("Upgrade", "websocket")
            self.send_header("Sec-WebSocket-Accept", "test-accept")
            self.end_headers()
            self.wfile.write(b"\x81\x02ok")
            self.wfile.flush()
            self.close_connection = True

        def log_message(self, fmt, *args):
            return

    with _running_server(TargetHandler) as target:
        with _running_server(make_handler(BridgeBrokerState("secret"))) as broker:
            config = port_forward.LocalForwardConfig(
                bridge_origin=port_forward.Origin("http", "127.0.0.1", broker.server_address[1]),
                bridge_path=port_forward.FORWARD_PATH,
                target_origin=port_forward.Origin("http", "127.0.0.1", target.server_address[1]),
                token="secret",
                local_origin="http://127.0.0.1:18080",
            )
            with _running_server(port_forward._make_local_handler(config)) as local:
                client = socket.create_connection(("127.0.0.1", local.server_address[1]), timeout=5)
                client.sendall(
                    b"GET /socket?q=1 HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Connection: Upgrade\r\n"
                    b"Upgrade: websocket\r\n"
                    b"Sec-WebSocket-Version: 13\r\n"
                    b"Sec-WebSocket-Key: dGVzdA==\r\n\r\n"
                )
                response = bytearray()
                while b"\x81\x02ok" not in response:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
                client.close()

    assert response.startswith(b"HTTP/1.1 101")
    assert response.endswith(b"\x81\x02ok")
    assert observed == {"path": "/socket?q=1", "upgrade": "websocket"}


def test_port_forward_cli_builds_target_origin(monkeypatch):
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(port_forward, "serve_local_forward", lambda url, **kwargs: calls.append({"url": url, **kwargs}))

    result = CliRunner().invoke(
        app,
        [
            "port",
            "forward",
            "https://example.test/aimux_bridge",
            "--local-port",
            "8080",
            "--remote-host",
            "app.internal",
            "--remote-port",
            "3000",
            "--remote-scheme",
            "https",
            "--token",
            "secret",
        ],
    )

    assert result.exit_code == 0
    assert calls == [{
        "url": "https://example.test/aimux_bridge",
        "local_host": "127.0.0.1",
        "local_port": 8080,
        "target_origin": "https://app.internal:3000",
        "token": "secret",
        "preserve_host": False,
        "rewrite_origin": True,
        "rewrite_secure_cookie": False,
    }]


def test_port_forward_cli_rejects_invalid_remote_port():
    result = CliRunner().invoke(
        app,
        [
            "port",
            "forward",
            "http://example.test",
            "--local-port",
            "8080",
            "--remote-port",
            "0",
            "--token",
            "secret",
        ],
    )

    assert result.exit_code == 1
    assert "--remote-port must be between 1 and 65535" in result.output
