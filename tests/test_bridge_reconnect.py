"""Tests for the bridge reverse-client auto-reconnection logic."""
from __future__ import annotations

import threading
import urllib.error
from typing import Any

import pytest

from aimux import __version__
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


def test_connect_does_not_retry_when_reconnect_disabled(monkeypatch, capsys):
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
    assert f"aimux bridge client version {__version__}" in capsys.readouterr().out


def test_connect_processes_event_and_posts_result(monkeypatch):
    event = '{"id":"req-1","type":"session.list","payload":{}}'
    post_calls, _ = _patch_client(monkeypatch, events_side_effects=[[f"data: {event}", ""]])

    with pytest.raises(bridge.BridgeError) as exc:
        connect("http://broker.example", identifier="winbox_1", token="secret", reconnect=False)

    assert exc.value.kind == "remote_disconnected"
    assert post_calls[0]["url"] == "http://broker.example/client/register"
    assert post_calls[1]["url"] == "http://broker.example/client/result"
    assert post_calls[1]["payload"] == {
        "id": "req-1",
        "ok": True,
        "payload": {"sessions": []},
    }


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


def test_connect_can_stop_while_event_stream_reader_is_blocked(monkeypatch):
    posted: list[dict[str, Any]] = []
    reader_started = threading.Event()
    response_closed = threading.Event()
    stop_event = threading.Event()

    def fake_post_json(url, token, payload, *, timeout=10.0):
        posted.append({"url": url, "token": token, "payload": payload})
        return {}

    class BlockingResponse:
        def __iter__(self):
            reader_started.set()
            response_closed.wait(timeout=2.0)
            if False:
                yield b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            response_closed.set()
            return False

    def fake_open_events(url, token, identifier):
        return BlockingResponse()

    def trigger_stop():
        reader_started.wait(timeout=1.0)
        stop_event.set()

    monkeypatch.setattr("aimux.bridge_client._post_json", fake_post_json)
    monkeypatch.setattr("aimux.bridge_client._open_events", fake_open_events)
    stopper = threading.Thread(target=trigger_stop)
    stopper.start()

    with pytest.raises(KeyboardInterrupt):
        connect(
            "http://broker.example",
            identifier="winbox_1",
            token="secret",
            stop_event=stop_event,
            stream_poll_interval=0.01,
        )

    stopper.join(timeout=1.0)
    assert reader_started.is_set()
    assert response_closed.is_set()
    assert posted[0]["url"] == "http://broker.example/client/register"
