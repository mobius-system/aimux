"""The reverse client's own data endpoints must honour a path-prefixed broker URL.

``https://host/aimux_bridge`` is a documented broker URL form (README's
``port forward`` example) and is how the Mobius gateway exposes the broker. The
data endpoints live under that prefix. Building ``/client/data`` by hand drops
it, and the request then reaches something that is not the broker — silently,
because a gateway 404 is just a response body nobody used to look at.
"""
from __future__ import annotations

import http.client
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aimux import bridge_client
from aimux.bridge_client import ReverseClientRuntime, _data_plane_target, _stream_file_upload
from aimux.bridge_server import BridgeBrokerState, make_handler


PREFIX = "/aimux_bridge"


class _Gateway:
    """Stand-in for the Mobius gateway: serves ``/aimux_bridge/*`` and 404s
    everything else, exactly like the reverse proxy in front of the broker."""

    def __init__(self, upstream_port: int) -> None:
        self._upstream_port = upstream_port
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = int(self._server.server_address[1])
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{PREFIX}"

    def _handler(self):
        upstream_port = self._upstream_port

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _forward(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                if not parsed.path.startswith(PREFIX):
                    # No route for this path — the gateway's own 404, which is
                    # what a dropped prefix actually hits.
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                target = parsed.path[len(PREFIX):] or "/"
                if parsed.query:
                    target += "?" + parsed.query
                length = int(self.headers.get("Content-Length") or 0)
                upstream = http.client.HTTPConnection("127.0.0.1", upstream_port, timeout=30)
                try:
                    upstream.putrequest(self.command, target)
                    for name, value in self.headers.items():
                        if name.lower() == "host":
                            continue
                        upstream.putheader(name, value)
                    upstream.endheaders()
                    if length:
                        upstream.send(self.rfile.read(length))
                    response = upstream.getresponse()
                    body = response.read()
                finally:
                    upstream.close()
                self.send_response(response.status)
                for name, value in response.getheaders():
                    if name.lower() in {"transfer-encoding", "content-length", "connection"}:
                        continue
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_DELETE = _forward

        return Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def data_plane():
    """A real broker behind a prefix-serving gateway, with a registered client."""
    state = BridgeBrokerState("test-token")
    broker = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    broker_port = int(broker.server_address[1])
    threading.Thread(target=broker.serve_forever, daemon=True).start()

    gateway = _Gateway(broker_port)
    runtime = ReverseClientRuntime("box")
    state.register_client(runtime.registration_payload(replace=False))
    state.connect_events("box")

    try:
        yield state, runtime, gateway
    finally:
        gateway.close()
        broker.shutdown()
        broker.server_close()


# ---------------------------------------------------------------------------
# The URL the data endpoints are addressed at
# ---------------------------------------------------------------------------


def test_data_plane_target_keeps_a_path_prefix():
    conn, base_path = _data_plane_target("https://gateway.example/aimux_bridge")
    conn.close()

    assert base_path == PREFIX


def test_data_plane_target_is_empty_without_a_prefix():
    conn, base_path = _data_plane_target("http://127.0.0.1:33315")
    conn.close()

    assert base_path == ""


def test_data_plane_target_tolerates_a_trailing_slash():
    conn, base_path = _data_plane_target("http://127.0.0.1:33315/aimux_bridge/")
    conn.close()

    assert base_path == PREFIX


# ---------------------------------------------------------------------------
# End to end through the gateway
# ---------------------------------------------------------------------------


def test_file_upload_reaches_the_broker_through_a_prefixed_url(data_plane, tmp_path):
    """The regression: 20 MiB used to leave the client and never arrive."""
    state, runtime, gateway = data_plane
    payload = bytes(range(256)) * 256  # 64 KiB
    local = tmp_path / "payload.bin"
    local.write_bytes(payload)

    request_id, q = state.open_read_stream("box", "/remote/payload.bin")

    _stream_file_upload(gateway.base_url, "test-token", request_id, local, runtime)

    received = bytearray()
    while True:
        item = q.get(timeout=10)
        if item is None:
            break
        assert not isinstance(item, dict), f"broker reported an error: {item}"
        received += item

    assert bytes(received) == payload


def test_file_upload_reports_a_rejected_connection_instead_of_claiming_success(data_plane, tmp_path, monkeypatch):
    """A non-broker reply must surface.

    The upload used to read the response and discard it, so a gateway 404 read
    as "streamed file" while the broker waited out its full timeout.
    """
    _state, runtime, gateway = data_plane
    local = tmp_path / "payload.bin"
    local.write_bytes(b"hello")

    reported: list[dict] = []
    monkeypatch.setattr(
        bridge_client,
        "_post_json",
        lambda url, token, payload, **kwargs: reported.append(payload),
    )

    # A prefix the gateway does not serve.
    _stream_file_upload(f"http://127.0.0.1:{gateway.port}/wrong_prefix", "test-token", "req-1", local, runtime)

    assert reported, "the client reported nothing back to the broker"
    assert reported[0]["ok"] is False
    assert "404" in reported[0]["error"]["message"]


def test_file_download_uses_the_prefixed_path(data_plane, tmp_path):
    """The mirror direction: the write stream's data connection is prefixed too."""
    from aimux.bridge_client import _stream_file_download

    state, runtime, gateway = data_plane
    payload = b"downloaded-through-the-gateway"
    request_id, q, ready, _result_q = state.open_write_stream("box", "/remote/out.bin", len(payload))

    target = tmp_path / "out.bin"
    worker = threading.Thread(
        target=_stream_file_download,
        args=(gateway.base_url, "test-token", request_id, target, runtime),
        daemon=True,
    )
    worker.start()

    # The broker signals readiness once its data connection is attached, so
    # only then is there a queue to feed.
    assert ready.wait(timeout=10) is True
    q.put(payload)
    q.put(None)
    worker.join(timeout=10)

    assert not worker.is_alive(), "the download did not finish"
    assert target.read_bytes() == payload
