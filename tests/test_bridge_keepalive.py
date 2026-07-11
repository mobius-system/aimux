"""Tests for broker TCP keepalive applied to accepted SSE sockets."""
from __future__ import annotations

import socket

from aimux import bridge_server


def test_apply_tcp_keepalive_enables_so_keepalive_and_tunes_probes():
    recorded: dict[tuple, object] = {}

    class FakeSock:
        def setsockopt(self, level, opt, value):
            recorded[(level, opt)] = int(value)

        def ioctl(self, control, payload):
            recorded[("ioctl", control)] = payload

    bridge_server._apply_tcp_keepalive(FakeSock())

    assert recorded[(socket.SOL_SOCKET, socket.SO_KEEPALIVE)] == 1
    if hasattr(socket, "TCP_KEEPIDLE"):
        assert recorded[(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)] == bridge_server.BRIDGE_TCP_KEEPIDLE
    if hasattr(socket, "TCP_KEEPINTVL"):
        assert recorded[(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)] == bridge_server.BRIDGE_TCP_KEEPINTVL
    if hasattr(socket, "TCP_KEEPCNT"):
        assert recorded[(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)] == bridge_server.BRIDGE_TCP_KEEPCNT


def test_apply_tcp_keepalive_tolerates_unsupported_socket():
    class BadSock:
        def setsockopt(self, *args, **kwargs):
            raise OSError("not supported")

        def ioctl(self, *args, **kwargs):
            raise OSError("not supported")

    # Must not raise even if every option is rejected by the socket.
    bridge_server._apply_tcp_keepalive(BadSock())


def test_broker_state_touch_updates_last_seen():
    state = bridge_server.BridgeBrokerState("secret")
    state.register_client({"identifier": "winbox_1", "platform": "linux", "profiles": []})
    before = state.clients["winbox_1"].last_seen
    # Touch should refresh last_seen without flipping connected state.
    state.touch("winbox_1")
    after = state.clients["winbox_1"].last_seen
    assert after is not None
    assert after >= before if before is not None else after is not None
    assert state.clients["winbox_1"].connected is False

    # Touching an unknown client is a no-op.
    state.touch("ghost")
    assert "ghost" not in state.clients
