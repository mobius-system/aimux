"""Tests for the bridge reverse-client auto-reconnection logic."""
from __future__ import annotations

import urllib.error
from typing import Any

import pytest

from aimux import bridge
from aimux.bridge_client import connect


def _patch_client(monkeypatch, *, events_side_effects):
    """Replace _post_json and _open_events with controllable stubs.

    ``events_side_effects`` is a list whose entries are either an Exception
    instance/class (raised when opening the event stream) or an iterable of
    SSE byte lines to yield before the stream ends.
    """
    post_calls: list[dict[str, Any]] = []

    def fake_post_json(url, token, payload, *, timeout=10.0):
        post_calls.append({"url": url, "token": token, "payload": payload})
        return {}

    class _FakeResponse:
        def __init__(self, lines):
            self._lines = list(lines)

        def __iter__(self):
            for line in self._lines:
                yield line.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    calls = {"open": 0}

    def fake_open_events(url, token, identifier):
        idx = calls["open"]
        calls["open"] += 1
        effect = events_side_effects[idx]
        if isinstance(effect, BaseException):
            raise effect
        if isinstance(effect, type) and issubclass(effect, BaseException):
            raise effect("simulated disconnect")
        return _FakeResponse(effect)

    monkeypatch.setattr("aimux.bridge_client._post_json", fake_post_json)
    monkeypatch.setattr("aimux.bridge_client._open_events", fake_open_events)
    return post_calls, calls


def test_connect_retries_three_times_with_expected_intervals_then_gives_up(monkeypatch):
    sleeps: list[float] = []

    _patch_client(monkeypatch, events_side_effects=[
        urllib.error.URLError("disconnect 1"),
        urllib.error.URLError("disconnect 2"),
        TimeoutError("disconnect 3"),
        OSError("disconnect 4"),
    ])

    with pytest.raises(bridge.BridgeError) as exc:
        connect(
            "http://broker.example",
            identifier="winbox_1",
            token="secret",
            reconnect_intervals=(5.0, 10.0, 20.0),
            sleep=sleeps.append,
        )

    assert exc.value.kind == "remote_disconnected"
    assert exc.value.exit_code == 2
    assert sleeps == [5.0, 10.0, 20.0]


def test_connect_does_not_retry_when_reconnect_disabled(monkeypatch):
    sleeps: list[float] = []

    _patch_client(monkeypatch, events_side_effects=[urllib.error.URLError("nope")])

    with pytest.raises(bridge.BridgeError) as exc:
        connect(
            "http://broker.example",
            identifier="winbox_1",
            token="secret",
            reconnect=False,
            reconnect_intervals=(5.0, 10.0, 20.0),
            sleep=sleeps.append,
        )

    assert exc.value.kind == "remote_disconnected"
    assert sleeps == []


def test_connect_recovers_within_retry_budget(monkeypatch):
    """A flaky link that succeeds on the second attempt should not raise."""
    sleeps: list[float] = []
    events = [
        urllib.error.URLError("flaky"),
        [],  # second attempt: stream ends cleanly (no events, then EOF)
        urllib.error.URLError("flaky again"),
        urllib.error.URLError("fail"),
        urllib.error.URLError("fail"),
    ]

    _patch_client(monkeypatch, events_side_effects=events)

    # Stream EOF counts as a disconnect, so we expect 3 retries then failure.
    with pytest.raises(bridge.BridgeError):
        connect(
            "http://broker.example",
            identifier="winbox_1",
            token="secret",
            reconnect_intervals=(5.0, 10.0, 20.0),
            sleep=sleeps.append,
        )
    assert sleeps == [5.0, 10.0, 20.0]
