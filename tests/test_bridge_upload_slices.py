"""File uploads travel as numbered slices, not as one big body.

A proxy in front of the broker (nginx, whose request buffering is on by
default) does not pass a request body upstream until it has received all of
it. One big POST therefore leaves the receiving side blind for the whole
upload and then hands it the file in a single burst -- a 66 MiB file showed
13 seconds of nothing before the first byte. Slices are small enough that each
one clears the proxy immediately, and they stay under a proxy's default body
size cap besides.
"""
from __future__ import annotations

import http.client
import threading
import urllib.parse
from pathlib import Path
from http.server import ThreadingHTTPServer

import pytest

from aimux import bridge_client
from aimux.bridge_client import ReverseClientRuntime, _stream_file_upload
from aimux.bridge_server import DATA_UPLOAD_SLICE_SIZE, BridgeBrokerState, make_handler


TOKEN = "test-token"


# ---------------------------------------------------------------------------
# Broker side: slices join into one stream, in order
# ---------------------------------------------------------------------------


@pytest.fixture
def broker():
    state = BridgeBrokerState(TOKEN)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    port = int(server.server_address[1])
    threading.Thread(target=server.serve_forever, daemon=True).start()

    runtime = ReverseClientRuntime("box")
    state.register_client(runtime.registration_payload(replace=False))
    events = state.connect_events("box")

    try:
        yield state, events, port
    finally:
        server.shutdown()
        server.server_close()


def _post_slice(
    port: int,
    request_id: str,
    payload: bytes,
    *,
    seq: int | None = None,
    final: bool | None = None,
    declare_length: bool = True,
) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    path = f"/client/data?id={urllib.parse.quote(request_id)}"
    if seq is not None:
        path += f"&seq={seq}"
    if final is not None:
        path += f"&final={1 if final else 0}"
    try:
        conn.putrequest("POST", path)
        conn.putheader("Authorization", f"Bearer {TOKEN}")
        if declare_length:
            conn.putheader("Content-Length", str(len(payload)))
        conn.endheaders()
        if payload:
            conn.send(payload)
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def _drain(q) -> bytes:
    received = bytearray()
    while True:
        item = q.get(timeout=10)
        if item is None:
            return bytes(received)
        assert not isinstance(item, dict), f"broker reported an error: {item}"
        received += item


def test_open_read_stream_advertises_a_slice_size(broker):
    """The remote only slices when the broker asks it to, so an older broker
    keeps the single-body behaviour without a version handshake."""
    state, events, _port = broker

    state.open_read_stream("box", "/remote/big.bin")

    request = events.get(timeout=10)
    assert request["type"] == "file.open_read"
    assert request["payload"]["path"] == "/remote/big.bin"
    assert request["payload"]["slice_size"] == DATA_UPLOAD_SLICE_SIZE


def test_slices_arrive_as_one_stream_in_order(broker):
    state, _events, port = broker
    request_id, q = state.open_read_stream("box", "/remote/big.bin")

    assert _post_slice(port, request_id, b"first-", seq=0, final=False) == 200
    assert _post_slice(port, request_id, b"second-", seq=1, final=False) == 200
    assert _post_slice(port, request_id, b"third", seq=2, final=True) == 200

    assert _drain(q) == b"first-second-third"


def test_a_single_body_without_seq_still_ends_the_stream(broker):
    """Clients that predate slicing send one body and no seq."""
    state, _events, port = broker
    request_id, q = state.open_read_stream("box", "/remote/old-client.bin")

    assert _post_slice(port, request_id, b"whole file") == 200

    assert _drain(q) == b"whole file"
    assert state.get_stream(request_id) is None


def test_an_out_of_order_slice_is_refused_and_the_stream_survives(broker):
    """Refused rather than queued: the bytes would land in the wrong place."""
    state, _events, port = broker
    request_id, q = state.open_read_stream("box", "/remote/big.bin")

    assert _post_slice(port, request_id, b"second", seq=1, final=False) == 400

    assert _post_slice(port, request_id, b"first-", seq=0, final=False) == 200
    assert _post_slice(port, request_id, b"second", seq=1, final=True) == 200
    assert _drain(q) == b"first-second"


def test_a_body_without_a_declared_length_counts_as_empty(broker):
    """Reading to EOF instead would hang: a kept-alive connection has none."""
    state, _events, port = broker
    request_id, q = state.open_read_stream("box", "/remote/empty-tail.bin")

    assert _post_slice(port, request_id, b"", seq=0, final=False, declare_length=False) == 200
    assert _post_slice(port, request_id, b"tail", seq=1, final=True) == 200

    assert _drain(q) == b"tail"


# ---------------------------------------------------------------------------
# Client side: the framing it puts on the wire
# ---------------------------------------------------------------------------


class _FakeSocket:
    """Collects what the client writes and answers each request as it lands."""

    def __init__(self, responder) -> None:
        self.wire = bytearray()
        self.requests: list[tuple[str, bytes]] = []
        self._responder = responder
        self._pending = bytearray()
        self._to_read = bytearray()
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.wire.extend(data)
        self._pending.extend(data)
        self._serve_complete_requests()

    def _serve_complete_requests(self) -> None:
        while True:
            head, separator, rest = bytes(self._pending).partition(b"\r\n\r\n")
            if not separator:
                return
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip())
            if len(rest) < length:
                return
            self._pending = bytearray(rest[length:])
            self.requests.append((head.split(b"\r\n")[0].decode(), rest[:length]))
            self._to_read.extend(self._responder(len(self.requests), rest[:length]))

    def recv(self, size: int) -> bytes:
        if not self._to_read:
            raise AssertionError("the client read past the responses it was given")
        out = bytes(self._to_read[:size])
        del self._to_read[:size]
        return out

    def close(self) -> None:
        self.closed = True


class _FakeConnection:
    host = "gateway.example"
    port = 443

    def __init__(self, sock: _FakeSocket) -> None:
        self.sock = sock

    def connect(self) -> None:
        return None

    def close(self) -> None:
        if self.sock is not None:
            self.sock.close()
        self.sock = None


def _install_fake_transport(monkeypatch, responder=None) -> list[_FakeSocket]:
    """Route the data plane at a fake socket; returns the sockets opened."""
    sockets: list[_FakeSocket] = []

    def fake_target(base: str):
        sock = _FakeSocket(responder or (lambda index, body: b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"))
        sockets.append(sock)
        return _FakeConnection(sock), ""

    monkeypatch.setattr(bridge_client, "_data_plane_target", fake_target)
    return sockets


def _written_slices(sock: _FakeSocket) -> list[tuple[str, bytes]]:
    return [(line, body) for line, body in sock.requests]


@pytest.mark.parametrize("size", [DATA_UPLOAD_SLICE_SIZE * 2, DATA_UPLOAD_SLICE_SIZE * 2 + 7])
def test_every_slice_is_numbered_and_the_last_one_is_final(monkeypatch, tmp_path, size):
    """The broker only closes the file on the slice that says it is the end."""
    sockets = _install_fake_transport(monkeypatch)
    source = tmp_path / "payload.bin"
    payload = bytes(range(256)) * (size // 256) + b"\x00" * (size % 256)
    source.write_bytes(payload)

    _stream_file_upload("http://gateway.example", TOKEN, "req-1", source, ReverseClientRuntime("box"), DATA_UPLOAD_SLICE_SIZE)

    assert len(sockets) == 1
    sent = _written_slices(sockets[0])
    expected_slices = -(-size // DATA_UPLOAD_SLICE_SIZE)
    assert len(sent) == expected_slices
    assert [line.split(" ")[1].split("&")[-2:] for line, _ in sent] == [
        [f"seq={index}", "final=1" if index == expected_slices - 1 else "final=0"]
        for index in range(expected_slices)
    ]
    assert b"".join(body for _line, body in sent) == payload


def test_a_file_that_fits_the_slice_size_ends_on_that_slice(monkeypatch, tmp_path):
    """No empty tail request: the size from stat already marks the end."""
    sockets = _install_fake_transport(monkeypatch)
    source = tmp_path / "exact.bin"
    source.write_bytes(b"x" * DATA_UPLOAD_SLICE_SIZE)

    _stream_file_upload("http://gateway.example", TOKEN, "req-2", source, ReverseClientRuntime("box"), DATA_UPLOAD_SLICE_SIZE)

    assert len(sockets[0].requests) == 1
    assert "final=1" in sockets[0].requests[0][0]


def test_a_shrinking_file_still_gets_an_end_marker(monkeypatch, tmp_path):
    """The size from stat can be stale; the read hitting EOF must still close
    the file, or the broker waits for a slice that never comes."""
    sockets = _install_fake_transport(monkeypatch)
    source = tmp_path / "shrunk.bin"
    source.write_bytes(b"y" * DATA_UPLOAD_SLICE_SIZE)

    class _StaleStat:
        st_size = DATA_UPLOAD_SLICE_SIZE * 3

    monkeypatch.setattr(Path, "stat", lambda self: _StaleStat())

    _stream_file_upload(
        "http://gateway.example", TOKEN, "req-3", source, ReverseClientRuntime("box"), DATA_UPLOAD_SLICE_SIZE
    )

    lines = [line for line, _body in sockets[0].requests]
    assert len(lines) == 2  # the one slice the file had, plus an end marker
    assert "final=1" in lines[-1]


def test_a_proxy_that_closes_the_connection_is_reconnected(monkeypatch, tmp_path):
    """Kept-alive connections have request limits; the upload must survive."""
    def responder(index: int, body: bytes) -> bytes:
        if index == 2:
            return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
        return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}"

    sockets = _install_fake_transport(monkeypatch, responder)
    source = tmp_path / "payload.bin"
    payload = b"z" * (DATA_UPLOAD_SLICE_SIZE * 3)
    source.write_bytes(payload)

    _stream_file_upload("http://gateway.example", TOKEN, "req-4", source, ReverseClientRuntime("box"), DATA_UPLOAD_SLICE_SIZE)

    assert len(sockets) > 1, "the client kept using a connection the proxy closed"
    delivered: dict[int, bytes] = {}
    for sock in sockets:
        for line, body in sock.requests:
            seq = int(line.split("seq=")[1].split(" ")[0].split("&")[0])
            delivered[seq] = body
    assert b"".join(delivered[index] for index in sorted(delivered)) == payload


def test_a_proxy_that_always_closes_does_not_loop_forever(monkeypatch, tmp_path):
    """Re-sending is bounded; a proxy that cannot carry the upload is an error."""
    def responder(index: int, body: bytes) -> bytes:
        return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"

    _install_fake_transport(monkeypatch, responder)
    source = tmp_path / "payload.bin"
    source.write_bytes(b"z" * (DATA_UPLOAD_SLICE_SIZE * 9))

    reported: list[dict] = []
    monkeypatch.setattr(
        bridge_client,
        "_post_json",
        lambda url, token, payload, **kwargs: reported.append(payload),
    )

    _stream_file_upload("http://gateway.example", TOKEN, "req-6", source, ReverseClientRuntime("box"), DATA_UPLOAD_SLICE_SIZE)

    assert reported and reported[0]["ok"] is False
    assert "closing" in reported[0]["error"]["message"]


def test_a_rejected_slice_is_reported_to_the_broker(monkeypatch, tmp_path):
    """A non-broker reply must surface instead of looking like a clean upload."""
    _install_fake_transport(monkeypatch, lambda index, body: b"HTTP/1.1 413 Too Large\r\nContent-Length: 0\r\n\r\n")
    source = tmp_path / "payload.bin"
    source.write_bytes(b"q" * (DATA_UPLOAD_SLICE_SIZE + 1))

    reported: list[dict] = []
    monkeypatch.setattr(
        bridge_client,
        "_post_json",
        lambda url, token, payload, **kwargs: reported.append(payload),
    )

    _stream_file_upload("http://gateway.example", TOKEN, "req-5", source, ReverseClientRuntime("box"), DATA_UPLOAD_SLICE_SIZE)

    assert reported and reported[0]["ok"] is False
    assert "413" in reported[0]["error"]["message"]
