"""The broker tells clients which aimux version it runs.

Clients (TUI/desktop) read ``GET /api/health`` before connecting and install that
same version locally, so a client that is newer or older than the broker cannot
silently break the bridge. The field is additive: an older client ignores it, and
an older broker without it just reads as "unknown" on the client side.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from aimux import __version__
from aimux.bridge_server import BridgeBrokerState, make_handler


def _broker():
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(BridgeBrokerState("test-token")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def _get(port: int, path: str, token: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def test_health_reports_the_running_aimux_version():
    server, port = _broker()
    try:
        payload = _get(port, "/api/health", "test-token")
        assert payload["ok"] is True
        assert payload["version"] == __version__
    finally:
        server.shutdown()
        server.server_close()


def test_health_still_requires_the_broker_token():
    server, port = _broker()
    try:
        try:
            _get(port, "/api/health", "wrong-token")
        except urllib.error.HTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError("a wrong token must not be served")
    finally:
        server.shutdown()
        server.server_close()
