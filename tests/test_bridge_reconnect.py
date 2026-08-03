"""Tests for the bridge reverse-client auto-reconnection logic."""
from __future__ import annotations

import io
import threading
import urllib.error
from typing import Any

import pytest
from loguru import logger

from aimux import __version__
from aimux import bridge
from aimux import bridge_client
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


def test_connect_re_registers_before_each_event_stream_retry(monkeypatch):
    post_calls, open_calls = _patch_client(monkeypatch, events_side_effects=[
        urllib.error.URLError("disconnect 1"),
        urllib.error.URLError("disconnect 2"),
    ])

    with pytest.raises(bridge.BridgeError):
        connect(
            "http://broker.example",
            identifier="winbox_1",
            token="secret",
            reconnect_intervals=(0.0,),
            sleep=lambda _: None,
        )

    register_calls = [call for call in post_calls if call["url"] == "http://broker.example/client/register"]
    assert open_calls["open"] == 2
    assert len(register_calls) == 2
    assert register_calls[0]["payload"]["client_id"] == register_calls[1]["payload"]["client_id"]


def test_connect_does_not_retry_when_reconnect_disabled(monkeypatch):
    sleeps: list[float] = []
    logs: list[str] = []
    sink_id = logger.add(lambda message: logs.append(str(message)), format="{message}")

    _patch_client(monkeypatch, events_side_effects=[urllib.error.URLError("nope")])

    try:
        with pytest.raises(bridge.BridgeError) as exc:
            connect(
                "http://broker.example",
                identifier="winbox_1",
                token="secret",
                reconnect=False,
                reconnect_intervals=(5.0, 10.0, 20.0),
                sleep=sleeps.append,
            )
    finally:
        logger.remove(sink_id)

    assert exc.value.kind == "remote_disconnected"
    assert sleeps == []
    assert any(f"aimux bridge client version {__version__}" in line for line in logs)


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


def test_handle_event_logs_bridge_command_details(monkeypatch):
    posted: list[dict[str, Any]] = []
    logs: list[str] = []
    sink_id = logger.add(lambda message: logs.append(str(message)), format="{message}")

    class FakeRuntime:
        identifier = "winbox_1"

        def handle(self, message_type, payload):
            assert message_type == "file.write"
            assert payload["path"] == r"C:\tmp\out.txt"
            return {"size": 5}

    def fake_post_json(url, token, payload, *, timeout=10.0):
        posted.append({"url": url, "token": token, "payload": payload})
        return {}

    monkeypatch.setattr("aimux.bridge_client._post_json", fake_post_json)

    try:
        bridge_client._handle_event(
            FakeRuntime(),
            "http://broker.example",
            "secret",
            {
                "id": "req-1",
                "type": "file.write",
                "payload": {"path": r"C:\tmp\out.txt", "content_b64": "aGVsbG8=", "max_size": 100},
            },
        )
    finally:
        logger.remove(sink_id)

    joined = "\n".join(logs)
    assert "bridge client received command" in joined
    assert "type=file.write" in joined
    assert "C:\\\\tmp\\\\out.txt" in joined
    assert "content_b64_len" in joined
    assert "bridge client completed command" in joined
    assert posted[0]["payload"] == {"id": "req-1", "ok": True, "payload": {"size": 5}}


def test_serve_event_stream_logs_heartbeat(monkeypatch):
    logs: list[str] = []
    sink_id = logger.add(lambda message: logs.append(str(message)), format="{message}")

    class FakeRuntime:
        identifier = "winbox_1"
        sessions = {}
        adapters = {"cmd": object()}

    class FakeReader:
        calls = 0

        def __init__(self, response):
            pass

        def start(self):
            pass

        def get(self, timeout):
            self.calls += 1
            if self.calls == 1:
                raise bridge_client.queue.Empty
            return KeyboardInterrupt()

    times = iter([0.0, 181.0, 182.0])
    monkeypatch.setattr(bridge_client, "_EventStreamReader", FakeReader)
    monkeypatch.setattr(bridge_client.time, "monotonic", lambda: next(times))

    try:
        with pytest.raises(KeyboardInterrupt):
            bridge_client._serve_event_stream(
                object(),
                runtime=FakeRuntime(),
                base="http://broker.example",
                token="secret",
                stop_event=None,
                poll_interval=0.01,
                heartbeat_interval=180.0,
            )
    finally:
        logger.remove(sink_id)

    assert any("bridge client heartbeat identifier=winbox_1" in line for line in logs)


def _patch_register(monkeypatch, *, post_side_effects):
    """Stub _post_json/_open_events to drive register-side behavior.

    ``post_side_effects`` is a list whose entries are either an Exception
    instance/class (raised by _post_json) or a dict (returned on success).
    Once exhausted, the last entry repeats. The event stream always fails
    immediately so tests can focus on the register phase.
    """
    calls: list[dict[str, Any]] = []

    def fake_post_json(url, token, payload, *, timeout=10.0):
        idx = len(calls)
        calls.append({"url": url, "payload": payload})
        effect = post_side_effects[idx] if idx < len(post_side_effects) else post_side_effects[-1]
        if isinstance(effect, BaseException):
            raise effect
        if isinstance(effect, type) and issubclass(effect, BaseException):
            raise effect("simulated")
        return effect

    def fake_open_events(url, token, identifier):
        raise urllib.error.URLError("stream closed")

    monkeypatch.setattr("aimux.bridge_client._post_json", fake_post_json)
    monkeypatch.setattr("aimux.bridge_client._open_events", fake_open_events)
    return calls


def _http_error(code: int, msg: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://broker.example/client/register",
        code,
        msg,
        {},
        io.BytesIO(b"{}"),
    )


def test_register_retries_on_409_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    calls = _patch_register(monkeypatch, post_side_effects=[
        _http_error(409, "Conflict"),
        {},  # second register attempt succeeds
    ])
    with pytest.raises(bridge.BridgeError) as exc:
        connect(
            "http://broker.example",
            identifier="winbox_1",
            token="secret",
            reconnect=True,
            reconnect_intervals=(),  # do not retry the SSE stage
            register_retry_intervals=(2.0, 4.0),
            sleep=sleeps.append,
        )
    # Register recovered on attempt 2; then the event stream failed and, with
    # no SSE retries configured, connect raises remote_disconnected.
    assert exc.value.kind == "remote_disconnected"
    assert len(calls) == 2
    assert sleeps == [2.0]


def test_register_gives_up_after_retries(monkeypatch):
    sleeps: list[float] = []
    calls = _patch_register(monkeypatch, post_side_effects=[_http_error(409, "Conflict")])
    with pytest.raises(bridge.BridgeError) as exc:
        connect(
            "http://broker.example",
            identifier="winbox_1",
            token="secret",
            register_retry_intervals=(2.0, 4.0, 8.0),
            sleep=sleeps.append,
        )
    assert exc.value.kind == "remote_disconnected"
    assert exc.value.exit_code == 2
    assert len(calls) == 4  # 1 initial + 3 retries
    assert sleeps == [2.0, 4.0, 8.0]


def test_register_409_does_not_retry_when_reconnect_disabled(monkeypatch):
    sleeps: list[float] = []
    calls = _patch_register(monkeypatch, post_side_effects=[_http_error(409, "Conflict")])
    with pytest.raises(bridge.BridgeError) as exc:
        connect(
            "http://broker.example",
            identifier="winbox_1",
            token="secret",
            reconnect=False,
            register_retry_intervals=(2.0, 4.0),
            sleep=sleeps.append,
        )
    assert exc.value.kind == "remote_disconnected"
    assert len(calls) == 1
    assert sleeps == []


def test_register_auth_failure_is_not_retried(monkeypatch):
    sleeps: list[float] = []
    calls = _patch_register(monkeypatch, post_side_effects=[_http_error(401, "Unauthorized")])
    with pytest.raises(bridge.BridgeError) as exc:
        connect(
            "http://broker.example",
            identifier="winbox_1",
            token="secret",
            register_retry_intervals=(2.0, 4.0),
            sleep=sleeps.append,
        )
    assert exc.value.kind == "auth_required"
    assert exc.value.exit_code == 4
    assert len(calls) == 1
    assert sleeps == []


def test_reconnect_intervals_meet_persistence_sla():
    """RECONNECT_INTERVALS must retry for >=30 min with max 1 min gaps.

    User SLA: bridge reverse connect must not give up on transient disconnects
    during long-running remote commands (was 3 retries / ~35s, killed remote
    commands mid-flight).
    """
    intervals = bridge_client.RECONNECT_INTERVALS
    assert sum(intervals) >= 1800, f"reconnect window {sum(intervals):.0f}s < 30min"
    assert max(intervals) <= 60, f"max interval {max(intervals):.0f}s > 60s"
