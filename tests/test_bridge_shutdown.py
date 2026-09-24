"""Shutting the event stream down must not wait for the next frame.

Closing a buffered HTTP response takes the same lock the SSE reader holds
while blocked in ``recv``, so on a quiet stream ``response.close()`` stalls
until the broker sends something — up to a full heartbeat interval. Ctrl+C
therefore looked "slow to exit" at random. ``_unblock_response`` shuts the
socket down first so that read returns immediately.
"""
from __future__ import annotations

import socket
import threading
import time

from aimux.bridge_client import _unblock_response


def test_unblock_response_makes_a_blocked_reader_return():
    local, peer = socket.socketpair()
    reader = local.makefile("rb")
    returned = threading.Event()

    def block_on_read() -> None:
        reader.read(1)  # nothing is ever sent; only the shutdown frees this
        returned.set()

    thread = threading.Thread(target=block_on_read, daemon=True)
    thread.start()
    time.sleep(0.2)
    assert not returned.is_set(), "precondition: the reader should be blocked"

    _unblock_response(reader)

    assert returned.wait(timeout=5) is True, "the reader stayed blocked after shutdown"
    reader.close()
    peer.close()
    local.close()


def test_unblock_response_leaves_the_descriptor_intact():
    """It duplicates the descriptor before shutting it down, so the response
    object it is about to close still owns a working fd."""
    local, peer = socket.socketpair()
    try:
        _unblock_response(local)
        local.fileno()  # raises if the socket object was disturbed
    finally:
        local.close()
        peer.close()


def test_unblock_response_tolerates_objects_without_a_fileno():
    class NoFileno:
        def fileno(self):
            raise OSError("no fd here")

    _unblock_response(NoFileno())
    _unblock_response(object())
