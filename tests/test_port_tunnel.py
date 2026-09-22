"""Tests for the client-to-client HTTP port tunnel (port_tunnel + broker plumbing)."""
from __future__ import annotations

import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aimux import bridge
from aimux import port_tunnel
from aimux.bridge_client import ReverseClientRuntime
from aimux.bridge_server import (
    HTTP_TUNNEL_QUEUE_DEPTH,
    BridgeBrokerState,
    HttpPortTunnel,
    _iter_chunked,
    _put_with_backpressure,
    parse_allow_http_ports,
)


# ---------------------------------------------------------------------------
# Port list parsing
# ---------------------------------------------------------------------------


def test_parse_port_list_accepts_commas_and_whitespace():
    assert port_tunnel.parse_port_list("8000, 8001,8002") == (8000, 8001, 8002)


def test_parse_port_list_dedupes_and_keeps_order():
    assert port_tunnel.parse_port_list("9000,8000,9000") == (9000, 8000)


def test_parse_port_list_treats_empty_as_no_ports():
    assert port_tunnel.parse_port_list(None) == ()
    assert port_tunnel.parse_port_list("") == ()
    assert port_tunnel.parse_port_list(" , ") == ()


@pytest.mark.parametrize("value", ["8000,abc", "0", "70000", "-1"])
def test_parse_port_list_rejects_malformed_entries(value):
    with pytest.raises(bridge.BridgeError):
        port_tunnel.parse_port_list(value)


def test_parse_allow_http_ports_fails_closed_on_garbage():
    assert parse_allow_http_ports(None) == frozenset()
    assert parse_allow_http_ports("8000") == frozenset()
    assert parse_allow_http_ports([8000, "8001", 0, 70000, "x"]) == frozenset({8000, 8001})


# ---------------------------------------------------------------------------
# Broker allowlist enforcement
# ---------------------------------------------------------------------------


def _state_with_client(*, allow=(18000,), connected=True) -> tuple[BridgeBrokerState, ReverseClientRuntime]:
    state = BridgeBrokerState("tok")
    runtime = ReverseClientRuntime("a-box", allow_http_ports=tuple(allow))
    state.register_client(runtime.registration_payload(replace=False))
    if connected:
        state.connect_events("a-box")
    return state, runtime


def test_registration_advertises_allow_http_ports():
    runtime = ReverseClientRuntime("a-box", allow_http_ports=(8000, 8001))
    assert runtime.registration_payload(replace=False)["allow_http_ports"] == [8000, 8001]
    assert runtime.http_port_allowed(8000) is True
    assert runtime.http_port_allowed(8001) is True
    assert runtime.http_port_allowed(8002) is False


def test_registration_without_allow_http_ports_is_unreachable_by_default():
    runtime = ReverseClientRuntime("a-box")
    assert runtime.registration_payload(replace=False)["allow_http_ports"] == []
    assert runtime.http_port_allowed(8000) is False


def test_broker_refuses_port_outside_allowlist():
    state, _ = _state_with_client(allow=(18000,))

    with pytest.raises(bridge.BridgeError) as excinfo:
        state.create_http_port_tunnel({"remote_client": "a-box", "port": 18001}, timeout=1)

    assert excinfo.value.kind == "port_not_allowed"
    assert "18000" in excinfo.value.message  # tells the operator what IS allowed
    assert state.list_http_tunnels() == []


def test_broker_refuses_every_port_when_client_declared_none():
    state, _ = _state_with_client(allow=())

    with pytest.raises(bridge.BridgeError) as excinfo:
        state.create_http_port_tunnel({"remote_client": "a-box", "port": 18000}, timeout=1)

    assert excinfo.value.kind == "port_not_allowed"
    assert "no --allow-http-ports" in excinfo.value.message


def test_broker_reports_unknown_and_disconnected_remotes():
    state = BridgeBrokerState("tok")
    with pytest.raises(bridge.BridgeError) as excinfo:
        state.create_http_port_tunnel({"remote_client": "ghost", "port": 18000}, timeout=1)
    assert excinfo.value.kind == "not_found"

    state, _ = _state_with_client(connected=False)
    with pytest.raises(bridge.BridgeError) as excinfo:
        state.create_http_port_tunnel({"remote_client": "a-box", "port": 18000}, timeout=1)
    assert excinfo.value.kind == "remote_disconnected"


def test_broker_validates_the_port_argument():
    state, _ = _state_with_client()
    for payload in ({"remote_client": "a-box"}, {"remote_client": "a-box", "port": 0}, {"port": 8000}):
        with pytest.raises(bridge.BridgeError) as excinfo:
            state.create_http_port_tunnel(payload, timeout=1)
        assert excinfo.value.kind == "bad_request"


def test_broker_drops_the_session_when_the_target_refuses(monkeypatch):
    state, _ = _state_with_client(allow=(18000,))

    def refuse(remote, message_type, payload, timeout):
        raise bridge.BridgeError("no listener", kind="port_unreachable", exit_code=2)

    monkeypatch.setattr(state, "submit", refuse)

    with pytest.raises(bridge.BridgeError):
        state.create_http_port_tunnel({"remote_client": "a-box", "port": 18000}, timeout=1)
    # A refused handshake must not leave a session behind for the sweep to find.
    assert state.list_http_tunnels() == []


# ---------------------------------------------------------------------------
# Tunnel session lifecycle
# ---------------------------------------------------------------------------


def test_finish_direction_signals_eof_and_closes_once_both_are_done():
    state, _ = _state_with_client()
    session_id = "s1"
    tunnel = HttpPortTunnel(session_id=session_id, remote="a-box", port=18000, created_at="now")
    state.http_tunnels[session_id] = tunnel

    state.finish_tunnel_direction(session_id, "up")
    assert tunnel.up.producer_done.is_set() is True
    assert tunnel.down.producer_done.is_set() is False
    assert state.list_http_tunnels() != []  # one half may still be draining

    state.finish_tunnel_direction(session_id, "down")
    assert tunnel.down.producer_done.is_set() is True
    assert state.list_http_tunnels() == []


def test_close_tunnel_abandons_both_pipes():
    state, _ = _state_with_client()
    tunnel = HttpPortTunnel(session_id="s2", remote="a-box", port=18000, created_at="now")
    state.http_tunnels["s2"] = tunnel

    state.close_http_tunnel("s2", reason="test")

    for pipe in (tunnel.up, tunnel.down):
        assert pipe.producer_done.is_set() is True
        assert pipe.abandoned.is_set() is True
    assert state.list_http_tunnels() == []


def test_close_tunnels_for_remote_ends_its_sessions_only():
    state, _ = _state_with_client()
    state.http_tunnels["s3"] = HttpPortTunnel(session_id="s3", remote="a-box", port=18000, created_at="now")
    state.http_tunnels["s4"] = HttpPortTunnel(session_id="s4", remote="other", port=18000, created_at="now")

    closed = state.close_http_tunnels_for_remote("a-box", reason="test")

    assert closed == 1
    assert [item["id"] for item in state.list_http_tunnels()] == ["s4"]


def test_disconnect_events_tears_down_that_remotes_tunnels():
    state, _ = _state_with_client()
    state.http_tunnels["s5"] = HttpPortTunnel(session_id="s5", remote="a-box", port=18000, created_at="now")

    q = state.clients["a-box"].queue
    state.disconnect_events("a-box", q)

    assert state.list_http_tunnels() == []


def test_abandoned_sessions_are_swept():
    state, _ = _state_with_client()
    stale = HttpPortTunnel(session_id="old", remote="a-box", port=18000, created_at="then")
    stale.last_activity = time.monotonic() - 10_000  # nobody ever attached
    state.http_tunnels["old"] = stale

    state._sweep_http_tunnels_locked()

    assert state.list_http_tunnels() == []
    assert stale.up.abandoned.is_set() is True


def test_tunnel_queues_are_bounded():
    tunnel = HttpPortTunnel(session_id="s6", remote="a-box", port=18000, created_at="now")
    for _ in range(HTTP_TUNNEL_QUEUE_DEPTH):
        assert _put_with_backpressure(tunnel.up, b"x") is True
    assert tunnel.up.chunks.full() is True


def test_backpressure_never_drops_payload():
    """A full queue must block the producer, not discard bytes.

    End-of-stream used to be forced into the queue as a sentinel, which meant
    dropping the oldest chunk to make room and silently truncating any
    transfer larger than the queue depth.
    """
    tunnel = HttpPortTunnel(session_id="s7", remote="a-box", port=18000, created_at="now")
    for index in range(HTTP_TUNNEL_QUEUE_DEPTH):
        assert _put_with_backpressure(tunnel.up, f"chunk-{index}".encode()) is True

    assert tunnel.up.chunks.full() is True
    drained = [tunnel.up.chunks.get_nowait() for _ in range(HTTP_TUNNEL_QUEUE_DEPTH)]
    assert drained[0] == b"chunk-0"  # nothing was discarded to make room
    assert len(drained) == HTTP_TUNNEL_QUEUE_DEPTH


def test_backpressure_gives_up_once_the_tunnel_is_abandoned():
    """Otherwise a producer blocked on a full queue waits for a reader that
    will never come back, holding a broker handler thread forever."""
    tunnel = HttpPortTunnel(session_id="s8", remote="a-box", port=18000, created_at="now")
    for _ in range(HTTP_TUNNEL_QUEUE_DEPTH):
        _put_with_backpressure(tunnel.up, b"x")

    tunnel.up.abandoned.set()

    assert _put_with_backpressure(tunnel.up, b"more") is False


# ---------------------------------------------------------------------------
# Chunked request bodies (how both aimux halves send their uplink)
# ---------------------------------------------------------------------------


def test_iter_chunked_decodes_framing_and_trailers():
    body = b"5\r\nhello\r\n6\r\n world\r\n0\r\nX-Trailer: 1\r\n\r\n"
    assert list(_iter_chunked(io.BytesIO(body))) == [b"hello", b" world"]


def test_iter_chunked_stops_at_a_truncated_body():
    assert list(_iter_chunked(io.BytesIO(b"5\r\nhel"))) == []


def test_iter_chunked_rejects_a_bad_size():
    with pytest.raises(bridge.BridgeError):
        list(_iter_chunked(io.BytesIO(b"zz\r\nhello\r\n")))


# ---------------------------------------------------------------------------
# End-to-end: two halves relaying real bytes through a real broker
# ---------------------------------------------------------------------------


class _EchoServer:
    """Stand-in for the HTTP service living on the target client's loopback."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.port = int(self._server.server_address[1])
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @staticmethod
    def _handler():
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):  # noqa: N802
                payload = b"echo:" + self.path.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _Broker:
    """A real broker on a real socket, driven over real HTTP."""

    def __init__(self) -> None:
        from aimux import bridge_server

        self.token = "test-token"
        self.state = BridgeBrokerState(self.token)
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), bridge_server.make_handler(self.state))
        self.port = int(self._server.server_address[1])
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def post(self, path: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.url}{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read().decode())

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _TargetClient:
    """Plays the reverse client that owns the port: consumes its SSE control
    stream and, on ``http_port.open``, dials loopback and relays."""

    def __init__(self, broker: _Broker, runtime: ReverseClientRuntime) -> None:
        self.broker = broker
        self.runtime = runtime
        self.dialled: list[int] = []
        broker.state.register_client(runtime.registration_payload(replace=False))
        self.queue = broker.state.connect_events(runtime.identifier)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self.queue.get()
            if item is None:
                return
            if item.get("type") != "http_port.open":
                self.broker.state.complete({"id": item["id"], "ok": False, "error": {"kind": "unsupported"}})
                continue
            payload = item["payload"]
            port = int(payload["port"])
            if not self.runtime.http_port_allowed(port):
                self.broker.state.complete(
                    {"id": item["id"], "ok": False, "error": {"kind": "port_not_allowed", "message": "nope", "code": 1}}
                )
                continue
            try:
                sock = socket.create_connection(("127.0.0.1", port), timeout=5)
            except OSError as exc:
                self.broker.state.complete(
                    {"id": item["id"], "ok": False, "error": {"kind": "port_unreachable", "message": str(exc), "code": 2}}
                )
                continue
            self.broker.state.complete({"id": item["id"], "ok": True, "payload": {"session_id": payload["session_id"]}})
            self.dialled.append(port)
            threading.Thread(
                target=self._relay, args=(str(payload["session_id"]), sock), daemon=True
            ).start()

    def _relay(self, session_id: str, sock: socket.socket) -> None:
        try:
            port_tunnel.relay_socket_over_bridge(
                base=self.broker.url,
                token=self.broker.token,
                session_id=session_id,
                sock=sock,
                prefix="/client",
            )
        finally:
            sock.close()

    def close(self) -> None:
        self.queue.put(None)


def _wait_until(predicate, *, timeout: float = 15.0) -> bool:
    """Sessions are reclaimed asynchronously once both directions drain, so
    assertions about a clean broker have to give that a moment."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def _open_reroute_side(broker: _Broker, *, remote: str, port: int) -> socket.socket:
    """Ask the broker for a tunnel and return the local socket end of it.

    Mirrors what ``local-http-port-reroute`` does for an accepted connection.
    """
    session = broker.post("/api/http-ports/open", {"remote_client": remote, "port": port})["session"]
    local, tunnel_side = socket.socketpair()
    threading.Thread(
        target=port_tunnel.relay_socket_over_bridge,
        kwargs={
            "base": broker.url,
            "token": broker.token,
            "session_id": session["id"],
            "sock": tunnel_side,
            "prefix": "/api",
        },
        daemon=True,
    ).start()
    return local


def _read_http_head_and_body(sock: socket.socket, *, timeout: float = 15.0) -> bytes:
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":", 1)[1].strip())
    while len(rest) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    return head + b"\r\n\r\n" + rest


@pytest.fixture
def tunnel_env():
    echo = _EchoServer()
    broker = _Broker()
    runtime = ReverseClientRuntime("a-box", allow_http_ports=(echo.port,))
    client = _TargetClient(broker, runtime)
    try:
        yield broker, client, echo
    finally:
        client.close()
        broker.close()
        echo.close()


def test_tunnel_carries_a_real_http_exchange(tunnel_env):
    broker, _client, echo = tunnel_env
    local = _open_reroute_side(broker, remote="a-box", port=echo.port)
    try:
        local.sendall(
            f"GET /hello HTTP/1.1\r\nHost: 127.0.0.1:{echo.port}\r\nConnection: close\r\n\r\n".encode()
        )
        response = _read_http_head_and_body(local)
    finally:
        local.close()

    assert b"200" in response.split(b"\r\n", 1)[0]
    assert b"echo:/hello" in response


def test_tunnel_opens_one_session_per_local_connection(tunnel_env):
    broker, _client, echo = tunnel_env
    for path in (b"/one", b"/two"):
        local = _open_reroute_side(broker, remote="a-box", port=echo.port)
        try:
            local.sendall(
                b"GET " + path + f" HTTP/1.1\r\nHost: 127.0.0.1:{echo.port}\r\nConnection: close\r\n\r\n".encode()
            )
            assert b"echo:" + path in _read_http_head_and_body(local)
        finally:
            local.close()
    # Both connections tunnelled independently and were reclaimed.
    assert _wait_until(lambda: broker.state.list_http_tunnels() == [])


def test_tunnel_propagates_the_peer_eof_back_to_the_local_socket(tunnel_env):
    broker, _client, echo = tunnel_env
    local = _open_reroute_side(broker, remote="a-box", port=echo.port)
    try:
        local.sendall(
            f"GET /bye HTTP/1.1\r\nHost: 127.0.0.1:{echo.port}\r\nConnection: close\r\n\r\n".encode()
        )
        _read_http_head_and_body(local)
        # The target closed the connection, so the tunnel must surface EOF here.
        local.settimeout(15)
        assert local.recv(4096) == b""
    finally:
        local.close()


def test_tunnel_refuses_a_port_outside_the_allowlist(tunnel_env):
    broker, _client, echo = tunnel_env
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        broker.post("/api/http-ports/open", {"remote_client": "a-box", "port": echo.port + 1})

    assert excinfo.value.code == 403
    body = json.loads(excinfo.value.read().decode())
    assert body["error"]["kind"] == "port_not_allowed"
    assert broker.state.list_http_tunnels() == []


def test_tunnel_reports_an_unreachable_target(tunnel_env):
    broker, client, echo = tunnel_env
    # Allowed by the client, but nothing is listening there. Both the broker's
    # record and the client's own check have to permit the port, otherwise this
    # would exercise the allowlist path instead of the dial path.
    broker.state.clients["a-box"].allow_http_ports = frozenset({echo.port, echo.port + 1})
    client.runtime.allow_http_ports = frozenset({echo.port, echo.port + 1})
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        broker.post("/api/http-ports/open", {"remote_client": "a-box", "port": echo.port + 1})

    assert excinfo.value.code == 502
    body = json.loads(excinfo.value.read().decode())
    assert body["error"]["kind"] == "port_unreachable"
    assert broker.state.list_http_tunnels() == []


def test_stream_endpoint_rejects_the_wrong_direction(tunnel_env):
    broker, _client, echo = tunnel_env
    # The reroute side reads "down"; asking for "up" is a protocol error, not a
    # silently-empty stream.
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(
            f"{broker.url}/api/http-ports/stream?id=nope&direction=up",
            timeout=10,
        )
    assert excinfo.value.code in (400, 401)


def test_stream_endpoint_rejects_an_unknown_session(tunnel_env):
    broker, _client, _echo = tunnel_env
    req = urllib.request.Request(
        f"{broker.url}/api/http-ports/stream?id=missing&direction=down",
        headers={"Authorization": f"Bearer {broker.token}"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=10)
    assert excinfo.value.code == 404


def test_stream_endpoint_requires_a_token(tunnel_env):
    broker, _client, _echo = tunnel_env
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(f"{broker.url}/api/http-ports", timeout=10)
    assert excinfo.value.code == 401


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


from typer.testing import CliRunner  # noqa: E402

from aimux import bridge_client, cli  # noqa: E402
from aimux.cli import app  # noqa: E402
from aimux.constants import EXIT_USER_ERROR  # noqa: E402


def _output(result) -> str:
    try:
        return result.output + result.stderr
    except ValueError:
        return result.output


def test_reverse_connect_passes_the_parsed_allow_http_ports(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        bridge_client,
        "connect",
        lambda url, **kwargs: captured.update({"url": url, **kwargs}),
    )

    result = CliRunner().invoke(
        app,
        ["reverse", "connect", "http://broker:8765", "--identifier", "a-box", "--token", "t",
         "--allow-http-ports", "8000, 8001,8000"],
    )

    assert result.exit_code == 0
    assert captured["allow_http_ports"] == (8000, 8001)


def test_reverse_connect_defaults_to_no_reachable_ports(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        bridge_client,
        "connect",
        lambda url, **kwargs: captured.update({"url": url, **kwargs}),
    )

    result = CliRunner().invoke(
        app,
        ["reverse", "connect", "http://broker:8765", "--identifier", "a-box", "--token", "t"],
    )

    assert result.exit_code == 0
    assert captured["allow_http_ports"] == ()


def test_reverse_connect_rejects_a_malformed_allow_http_ports():
    result = CliRunner().invoke(
        app,
        ["reverse", "connect", "http://broker:8765", "--identifier", "a-box", "--token", "t",
         "--allow-http-ports", "8000,nope"],
    )

    assert result.exit_code == EXIT_USER_ERROR
    assert "invalid port 'nope'" in _output(result)


@pytest.mark.parametrize(
    "args",
    [
        ["--local-port", "70000", "--remote-client-http-port", "8000"],
        ["--local-port", "9000", "--remote-client-http-port", "0"],
    ],
)
def test_local_http_port_reroute_validates_port_ranges(args):
    result = CliRunner().invoke(
        app,
        ["local-http-port-reroute", "--remote-client-id", "a-box", *args],
    )

    assert result.exit_code == EXIT_USER_ERROR
    assert "must be between" in _output(result)


def test_local_http_port_reroute_needs_a_broker_or_explicit_url(monkeypatch):
    monkeypatch.setattr(cli.bridge, "read_runtime", lambda: None)

    result = CliRunner().invoke(
        app,
        ["local-http-port-reroute", "--local-port", "9000",
         "--remote-client-id", "a-box", "--remote-client-http-port", "8000"],
    )

    assert result.exit_code == EXIT_USER_ERROR
    assert "no local bridge broker found" in _output(result)


def test_local_http_port_reroute_falls_back_to_runtime_json(monkeypatch):
    captured = {}
    runtime = bridge.BridgeRuntime(
        url="http://127.0.0.1:8765", token="from-runtime", pid=1, host="127.0.0.1", port=8765, created_at="now"
    )
    monkeypatch.setattr(cli.bridge, "read_runtime", lambda: runtime)
    monkeypatch.setattr(
        port_tunnel,
        "serve_local_http_reroute",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        app,
        ["local-http-port-reroute", "--local-port", "9000",
         "--remote-client-id", "a-box", "--remote-client-http-port", "8000"],
    )

    assert result.exit_code == 0
    assert captured == {
        "local_host": "127.0.0.1",
        "local_port": 9000,
        "base_url": "http://127.0.0.1:8765",
        "token": "from-runtime",
        "remote_client": "a-box",
        "remote_port": 8000,
    }


def test_local_http_port_reroute_prefers_explicit_url_and_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        cli.bridge, "read_runtime", lambda: pytest.fail("runtime.json should not be read")
    )
    monkeypatch.setattr(
        port_tunnel,
        "serve_local_http_reroute",
        lambda **kwargs: captured.update(kwargs),
    )

    result = CliRunner().invoke(
        app,
        ["local-http-port-reroute", "--local-port", "9000",
         "--remote-client-id", "a-box", "--remote-client-http-port", "8000",
         "--url", "https://broker/aimux", "--token", "tok"],
    )

    assert result.exit_code == 0
    assert captured["base_url"] == "https://broker/aimux"
    assert captured["token"] == "tok"


def test_local_http_port_reroute_reports_a_refused_start(monkeypatch):
    runtime = bridge.BridgeRuntime(
        url="http://127.0.0.1:8765", token="t", pid=1, host="127.0.0.1", port=8765, created_at="now"
    )
    monkeypatch.setattr(cli.bridge, "read_runtime", lambda: runtime)

    def refuse(**kwargs):
        raise bridge.BridgeError(
            "remote 'a-box' does not allow connections to port 8000", kind="port_not_allowed"
        )

    monkeypatch.setattr(port_tunnel, "serve_local_http_reroute", refuse)

    result = CliRunner().invoke(
        app,
        ["local-http-port-reroute", "--local-port", "9000",
         "--remote-client-id", "a-box", "--remote-client-http-port", "8000"],
    )

    assert result.exit_code == 1
    assert "does not allow connections to port 8000" in _output(result)


# ---------------------------------------------------------------------------
# Regressions: idle teardown and bulk integrity
# ---------------------------------------------------------------------------


class _TcpEchoServer:
    """Plain TCP echo — the tunnel is byte-transparent, so no HTTP needed."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = int(self._sock.getsockname()[1])
        self._stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn: socket.socket) -> None:
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    return
                conn.sendall(data)
        except OSError:
            return
        finally:
            conn.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def tcp_tunnel_env():
    echo = _TcpEchoServer()
    broker = _Broker()
    runtime = ReverseClientRuntime("a-box", allow_http_ports=(echo.port,))
    client = _TargetClient(broker, runtime)
    try:
        yield broker, client, echo
    finally:
        client.close()
        broker.close()
        echo.close()


def test_bulk_transfer_is_not_truncated(tcp_tunnel_env):
    """More data than the queue can hold must survive intact.

    The queue depth is 32 x 64 KiB, so 3 MiB cannot fit at once: this only
    passes if a full queue blocks the producer instead of dropping chunks.
    """
    broker, _client, echo = tcp_tunnel_env
    local = _open_reroute_side(broker, remote="a-box", port=echo.port)
    payload = bytes(range(256)) * (3 * 4096)  # 3 MiB, non-repeating enough to catch shifts
    received = bytearray()
    try:
        local.settimeout(60)

        def send():
            local.sendall(payload)
            local.shutdown(socket.SHUT_WR)

        threading.Thread(target=send, daemon=True).start()
        while len(received) < len(payload):
            chunk = local.recv(65536)
            if not chunk:
                break
            received += chunk
    finally:
        local.close()

    assert len(received) == len(payload)
    assert bytes(received) == payload


def test_idle_tunnel_is_closed_rather_than_left_hanging(tcp_tunnel_env, monkeypatch):
    """An idle session must unwind end to end.

    The broker used to exit this stream without an EOF/error frame and without
    closing the connection, leaving the peer's reader blocked on a socket that
    never closed — leaking a handler thread, a socket, and a relay thread per
    occurrence.
    """
    monkeypatch.setattr("aimux.bridge_server.HTTP_TUNNEL_IDLE_TIMEOUT", 0.5)
    broker, _client, echo = tcp_tunnel_env
    local = _open_reroute_side(broker, remote="a-box", port=echo.port)
    try:
        local.settimeout(30)
        # Nothing is sent in either direction.
        assert local.recv(4096) == b""
    finally:
        local.close()

    assert _wait_until(lambda: broker.state.list_http_tunnels() == [])
