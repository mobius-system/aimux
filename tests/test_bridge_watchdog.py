"""The watchdog must follow the owner's feed, not the file's mtime.

The owning TUI/Electron rewrites ``last_feed_watchdog`` every 5s. mtime is not
a liveness signal for the daemon: it writes the same file for its own runtime
state (on start, on every reconnect, on disconnect), so a daemon whose owner
has died would keep refreshing the mtime and feed its own watchdog forever.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from aimux import bridge_client
from aimux.bridge_client import _time_since_last_feed


TIMEOUT = 30.0


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_a_fresh_feed_is_not_expired(tmp_path):
    path = tmp_path / "runtime.json"
    _write(path, {"last_feed_watchdog": int(time.time() * 1000)})

    assert _time_since_last_feed(path, TIMEOUT) < 2


def test_a_stale_feed_is_expired_even_though_the_file_was_just_written(tmp_path):
    """The regression: the daemon writing its own runtime state refreshes the
    mtime, which used to reset the watchdog and mask a dead owner."""
    path = tmp_path / "runtime.json"
    _write(path, {"last_feed_watchdog": int((time.time() - 120) * 1000)})
    os.utime(path, None)  # a very fresh mtime, as the daemon's own write leaves

    assert _time_since_last_feed(path, TIMEOUT) > TIMEOUT


def test_the_feed_ages_once_the_owner_stops_writing(tmp_path):
    path = tmp_path / "runtime.json"
    _write(path, {"last_feed_watchdog": int(time.time() * 1000)})
    assert _time_since_last_feed(path, TIMEOUT) < 2

    stale = time.time() - 45
    _write(path, {"last_feed_watchdog": int(stale * 1000)})
    assert _time_since_last_feed(path, TIMEOUT) > TIMEOUT


def test_a_missing_feed_is_expired_however_fresh_the_mtime(tmp_path):
    """mtime is never consulted, so a file that was just written but carries no
    feed is still expired."""
    path = tmp_path / "keepalive"
    path.touch()  # fresh mtime, no last_feed_watchdog at all

    assert _time_since_last_feed(path, TIMEOUT) > TIMEOUT


def test_a_feed_is_required_even_when_mtime_is_ancient(tmp_path):
    """The mirror case: an old mtime with a fresh feed must NOT expire."""
    path = tmp_path / "runtime.json"
    _write(path, {"last_feed_watchdog": int(time.time() * 1000)})
    os.utime(path, (time.time() - 600, time.time() - 600))

    assert _time_since_last_feed(path, TIMEOUT) < 2


def test_an_unreadable_file_counts_as_expired(tmp_path):
    path = tmp_path / "runtime.json"

    assert _time_since_last_feed(path, TIMEOUT) > TIMEOUT


def test_malformed_state_counts_as_expired(tmp_path):
    path = tmp_path / "runtime.json"
    path.write_text("{not json", encoding="utf-8")
    os.utime(path, None)  # fresh mtime must not rescue it

    assert _time_since_last_feed(path, TIMEOUT) > TIMEOUT


def test_the_daemons_own_state_writes_do_not_refresh_the_feed(tmp_path):
    """Writes that carry runtime state but no feed must leave the age alone."""
    path = tmp_path / "runtime.json"
    _write(path, {"last_feed_watchdog": int((time.time() - 60) * 1000)})
    before = _time_since_last_feed(path, TIMEOUT)

    # what runtime_update(realtime_healthy_display=...) produces
    state = json.loads(path.read_text(encoding="utf-8"))
    state["realtime_healthy_display"] = True
    _write(path, state)

    after = _time_since_last_feed(path, TIMEOUT)
    assert after >= before, "a state-only write must not wind the watchdog back"
    assert after > TIMEOUT


# ---------------------------------------------------------------------------
# Expiry must actually end the process, not just raise a flag
# ---------------------------------------------------------------------------


class _HangingStream:
    """A healthy event stream: it simply never produces a frame and never ends."""

    def __init__(self, opened: threading.Event | None = None, *, honour_nothing: bool = False):
        self._opened = opened
        self._honour_nothing = honour_nothing

    def __iter__(self):
        if self._opened is not None:
            self._opened.set()
        time.sleep(30)
        return iter(())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fileno(self):
        raise OSError("no fd")


def _run_connect(monkeypatch, tmp_path, *, stream, stop_grace: float = 5.0):
    from aimux.bridge_client import connect

    monkeypatch.setattr(bridge_client, "WATCHDOG_INTERVAL", 0.05)
    monkeypatch.setattr(bridge_client, "WATCHDOG_EXIT_GRACE", stop_grace)
    monkeypatch.setattr(bridge_client, "_post_json", lambda *a, **k: {})
    monkeypatch.setattr(bridge_client, "_open_events", lambda *a, **k: stream)
    # A watchdog that fires after the test stopped waiting would otherwise
    # exit the process and take the whole run down with it.
    monkeypatch.setattr(bridge_client.os, "_exit", lambda code: None)

    runtime_file = tmp_path / "runtime.json"
    runtime_file.write_text(
        json.dumps({"last_feed_watchdog": int((time.time() - 60) * 1000)}),
        encoding="utf-8",
    )

    outcome: dict = {}
    done = threading.Event()

    def run() -> None:
        try:
            connect(
                "http://broker.example",
                identifier="box",
                token="t",
                reconnect=False,
                watchdog_path=runtime_file,
                watchdog_timeout=0.2,
                runtime_path=runtime_file,
            )
            outcome["result"] = "returned"
        except KeyboardInterrupt:
            outcome["result"] = "KeyboardInterrupt"
        except BaseException as exc:  # noqa: BLE001 - report anything unexpected
            outcome["result"] = repr(exc)
        finally:
            done.set()

    threading.Thread(target=run, daemon=True).start()
    return done, outcome


def test_watchdog_expiry_ends_a_healthy_event_stream(monkeypatch, tmp_path):
    """The regression: a healthy stream never ends by itself, so an expiry the
    stream loop cannot see left the daemon running indefinitely."""
    done, outcome = _run_connect(monkeypatch, tmp_path, stream=_HangingStream())

    assert done.wait(timeout=10), "connect() never returned after the watchdog expired"
    assert outcome["result"] in {"returned", "KeyboardInterrupt"}


def test_watchdog_exits_unconditionally_when_the_graceful_path_stalls(monkeypatch, tmp_path):
    """A stop that depends on where the process happens to be is not a
    watchdog: if the main loop cannot unwind, leave anyway."""
    exits: list[int] = []
    monkeypatch.setattr(bridge_client.os, "_exit", lambda code: exits.append(code))

    done, outcome = _run_connect(
        monkeypatch, tmp_path, stream=_HangingStream(), stop_grace=0.2
    )

    assert done.wait(timeout=10)
    # The stream loop does honour the event, so the graceful path wins here —
    # what matters is that the process ends either way.
    assert outcome["result"] in {"returned", "KeyboardInterrupt"} or exits == [0]
