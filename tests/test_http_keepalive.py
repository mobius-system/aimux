"""Keep-alive HTTP helper: one connection per origin, transparent reconnect."""
from __future__ import annotations

import http.server
import json
import threading

import pytest

from aimux import _http


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    close_after_response = False

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.served += 1
        body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_after_response:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        body = b'{"error": "nope"}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output clean
        pass


class _Server(http.server.ThreadingHTTPServer):
    def __init__(self, handler):
        super().__init__(("127.0.0.1", 0), handler)
        self.accepted = 0
        self.served = 0

    def get_request(self):
        sock, addr = super().get_request()
        self.accepted += 1
        return sock, addr


@pytest.fixture()
def serve():
    servers = []

    def start(*, close_after_response: bool = False):
        handler = type("Handler", (_Handler,), {"close_after_response": close_after_response})
        server = _Server(handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        return server

    _http.close_all()
    try:
        yield start
    finally:
        _http.close_all()
        for server, thread in servers:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


def test_requests_reuse_one_connection(serve):
    server = serve()
    url = f"http://127.0.0.1:{server.server_port}/api/health"

    for _ in range(3):
        status, _headers, body = _http.request("POST", url, {}, b"{}")

    assert status == 200 and json.loads(body)["ok"] is True
    assert server.accepted == 1, "a kept-alive connection must serve every request"


def test_reconnects_when_the_server_closed_the_socket(serve):
    # 'Connection: close' is what nginx does after its keepalive timeout; the
    # next call must create a fresh connection instead of failing.
    server = serve(close_after_response=True)
    url = f"http://127.0.0.1:{server.server_port}/x"

    assert _http.request("POST", url, {}, b"{}")[0] == 200
    assert _http.request("POST", url, {}, b"{}")[0] == 200
    assert server.accepted == 2


def test_each_thread_keeps_its_own_connection(serve):
    """MCP tool calls run on worker threads; a shared connection would serialize them."""
    server = serve()
    url = f"http://127.0.0.1:{server.server_port}/x"
    errors: list[BaseException] = []

    def worker():
        try:
            for _ in range(3):
                assert _http.request("POST", url, {}, b"{}")[0] == 200
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors, errors
    assert server.served == 6
    assert server.accepted == 2, "one persistent connection per worker thread"


def test_non_2xx_is_returned_not_raised(serve):
    server = serve()
    url = f"http://127.0.0.1:{server.server_port}/missing"

    status, _headers, body = _http.request("GET", url, {}, None)

    # The bridge maps status codes to its own errors; the helper must hand the
    # body back instead of raising like urllib did.
    assert status == 404 and json.loads(body)["error"] == "nope"
