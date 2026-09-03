"""Tests for the `aimux bridge restart` CLI command."""
from __future__ import annotations

import os
import signal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aimux import bridge
from aimux import bridge_server
from aimux.cli import app

runner = CliRunner()


class _FakeRuntime:
    def __init__(self, pid):
        self.url = "http://127.0.0.1:33315"
        self.token = "tok-1"
        self.pid = pid
        self.host = "127.0.0.1"
        self.port = 33315
        self.created_at = None


def _install(monkeypatch, *, pid, alive_pids, served, killed):
    monkeypatch.setattr(bridge, "read_runtime", lambda: _FakeRuntime(pid))
    monkeypatch.setattr(bridge_server, "serve", lambda host, port, token=None: served.append((host, port, token)))

    real_kill = os.kill

    def fake_kill(target, sig):
        if target == os.getpid():
            raise PermissionError("self")
        if target not in alive_pids:
            raise ProcessLookupError(target)
        killed.append((target, sig))
        if sig == signal.SIGTERM:
            alive_pids.discard(target)
        return real_kill(os.getpid(), 0)

    monkeypatch.setattr(os, "kill", fake_kill)


def test_restart_reserves_host_port_and_token(monkeypatch):
    alive, served, killed = {4242}, [], []
    _install(monkeypatch, pid=4242, alive_pids=alive, served=served, killed=killed)

    result = runner.invoke(app, ["bridge", "restart"])

    assert result.exit_code == 0, result.output
    assert killed == [(4242, signal.SIGTERM)]
    # The new broker must reuse the recorded bind and the SAME token so that
    # already-connected reverse clients keep authenticating after the restart.
    assert served == [("127.0.0.1", 33315, "tok-1")]


def test_restart_without_runtime_json_fails(monkeypatch):
    monkeypatch.setattr(bridge, "read_runtime", lambda: None)

    result = runner.invoke(app, ["bridge", "restart"])

    assert result.exit_code != 0
    assert "deploy" in result.output


def test_restart_refuses_to_kill_itself(monkeypatch):
    alive, served, killed = set(), [], []
    _install(monkeypatch, pid=os.getpid(), alive_pids=alive, served=served, killed=killed)

    result = runner.invoke(app, ["bridge", "restart"])

    assert result.exit_code != 0
    assert not killed and not served


def test_restart_recovers_from_stale_runtime_json(monkeypatch):
    # Old pid is already gone (ProcessLookupError on TERM): restart should
    # still bring a broker up on the recorded bind instead of erroring out.
    alive, served, killed = set(), [], []
    _install(monkeypatch, pid=99999, alive_pids=alive, served=served, killed=killed)

    result = runner.invoke(app, ["bridge", "restart"])

    assert result.exit_code == 0, result.output
    assert served == [("127.0.0.1", 33315, "tok-1")]


def test_restart_times_out_when_old_broker_ignores_sigterm(monkeypatch):
    alive, served, killed = {4242}, [], []
    _install(monkeypatch, pid=4242, alive_pids=alive, served=served, killed=killed)

    def stubborn_kill(target, sig):
        # SIGTERM is delivered but the pid never disappears.
        killed.append((target, sig))
        if sig == 0:
            raise ProcessLookupError(target) if target not in alive_pids else None

    monkeypatch.setattr(os, "kill", stubborn_kill)
    # Make the poll loop's target pid always look alive.
    monkeypatch.setattr(
        bridge_server, "serve", lambda host, port, token=None: served.append((host, port, token))
    )

    def always_alive(target, sig):
        killed.append((target, sig))
        return None

    monkeypatch.setattr(os, "kill", always_alive)

    result = runner.invoke(app, ["bridge", "restart", "--timeout", "0.3"])

    assert result.exit_code != 0
    assert not served
    assert "did not exit" in result.output
