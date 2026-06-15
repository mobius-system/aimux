from __future__ import annotations

import json

import pytest

pytest.importorskip("typer")
from typer.testing import CliRunner

from aimux import bridge
from aimux import session as session_mod
from aimux import ssh as ssh_mod
from aimux import tmux
from aimux.cli import app
from aimux.constants import EXIT_OK, EXIT_SAFETY_DENIED, EXIT_USER_ERROR


def _combined_output(result) -> str:
    try:
        return result.output + result.stderr
    except ValueError:
        return result.output


class FakeBridgeClient:
    def __init__(self):
        self.created = []
        self.sent = []
        self.killed = []

    def list_sessions(self, *, timeout=2.0):
        return []

    def create_session(self, **kwargs):
        self.created.append(kwargs)
        return {"session": {"id": f"{kwargs['remote']}/{kwargs['name']}"}}

    def send_keys(self, **kwargs):
        self.sent.append(kwargs)
        return {"ok": True}

    def capture(self, **kwargs):
        return {"content": "hello\n", "lines": kwargs["lines"], "ansi": False, "source": "screen-buffer"}

    def kill_session(self, **kwargs):
        self.killed.append(kwargs)
        return {"ok": True}


def test_new_bridge_remote_requires_profile(monkeypatch):
    fake = FakeBridgeClient()
    monkeypatch.setattr(tmux, "has_session", lambda name: False)
    monkeypatch.setattr(ssh_mod, "host_exists", lambda name: False)
    monkeypatch.setattr("aimux.cli._bridge_remote", lambda name: (fake, {"name": name, "status": "connected"}))

    result = CliRunner().invoke(app, ["new", "--remote", "winbox", "--name", "task"])

    assert result.exit_code == EXIT_USER_ERROR
    assert "--profile is required for bridge remote 'winbox'" in _combined_output(result)
    assert fake.created == []


def test_new_bridge_remote_uses_profile_and_does_not_create_tmux(monkeypatch):
    fake = FakeBridgeClient()
    monkeypatch.setattr(tmux, "has_session", lambda name: False)
    monkeypatch.setattr(ssh_mod, "host_exists", lambda name: False)
    monkeypatch.setattr("aimux.cli._bridge_remote", lambda name: (fake, {"name": name, "status": "connected"}))
    monkeypatch.setattr(tmux, "new_session", lambda *args, **kwargs: pytest.fail("tmux should not create bridge sessions"))

    result = CliRunner().invoke(
        app,
        ["new", "--remote", "winbox", "--profile", "cmd", "--name", "task", "--cmd", "cmd.exe"],
    )

    assert result.exit_code == EXIT_OK
    assert fake.created[0]["remote"] == "winbox"
    assert fake.created[0]["name"] == "task"
    assert fake.created[0]["profile"] == "cmd"
    assert fake.created[0]["cmd"] == "cmd.exe"


def test_new_ssh_remote_rejects_profile(monkeypatch):
    monkeypatch.setattr(tmux, "has_session", lambda name: False)
    monkeypatch.setattr(ssh_mod, "host_exists", lambda name: True)
    monkeypatch.setattr("aimux.cli._bridge_remote", lambda name: (None, None))

    result = CliRunner().invoke(app, ["new", "--remote", "worker", "--profile", "cmd", "--name", "task"])

    assert result.exit_code == EXIT_USER_ERROR
    assert "--profile is only valid for bridge remotes" in _combined_output(result)


def test_new_local_rejects_profile(monkeypatch):
    monkeypatch.setattr(tmux, "has_session", lambda name: False)

    result = CliRunner().invoke(app, ["new", "--profile", "cmd", "--name", "task"])

    assert result.exit_code == EXIT_USER_ERROR
    assert "--profile is only valid for bridge remotes" in _combined_output(result)


def _bridge_session() -> session_mod.Session:
    return session_mod.Session(
        name="task",
        attached=False,
        location="winbox",
        host="winbox",
        created_at="2026-06-15T00:00:00Z",
        created_by="tester",
        output_sync_file=None,
        managed=True,
        backend="bridge",
        transport="bridge",
        profile="cmd",
        client_status="connected",
    )


def test_attach_rejects_bridge_session(monkeypatch):
    monkeypatch.setattr(session_mod, "resolve", lambda spec: _bridge_session())
    monkeypatch.setattr(tmux, "attach", lambda *args, **kwargs: pytest.fail("tmux.attach should not run"))

    result = CliRunner().invoke(app, ["attach", "winbox/task"])

    assert result.exit_code == EXIT_SAFETY_DENIED
    assert "attach does not support bridge remote sessions" in _combined_output(result)


def test_send_keys_bridge_session_calls_bridge_client(monkeypatch):
    fake = FakeBridgeClient()
    monkeypatch.setattr(session_mod, "resolve", lambda spec: _bridge_session())
    monkeypatch.setattr(bridge.BridgeControlClient, "from_runtime", classmethod(lambda cls: fake))
    monkeypatch.setattr(tmux, "send_keys", lambda *args, **kwargs: pytest.fail("tmux.send_keys should not run"))

    result = CliRunner().invoke(app, ["send-keys", "winbox/task", "--", "dir", "Enter"])

    assert result.exit_code == EXIT_OK
    assert fake.sent == [{"remote": "winbox", "name": "task", "keys": ["dir", "Enter"], "flags": []}]


def test_capture_bridge_session_json_calls_bridge_client(monkeypatch):
    fake = FakeBridgeClient()
    monkeypatch.setattr(session_mod, "resolve", lambda spec: _bridge_session())
    monkeypatch.setattr(bridge.BridgeControlClient, "from_runtime", classmethod(lambda cls: fake))
    monkeypatch.setattr(tmux, "capture_pane", lambda *args, **kwargs: pytest.fail("tmux.capture should not run"))

    result = CliRunner().invoke(app, ["capture", "winbox/task", "--lines", "10", "--json"])

    assert result.exit_code == EXIT_OK
    payload = json.loads(result.stdout)
    assert payload == {
        "id": "winbox/task",
        "lines": 10,
        "content": "hello\n",
        "ansi": False,
        "source": "screen-buffer",
    }


def test_kill_bridge_session_calls_bridge_client(monkeypatch):
    fake = FakeBridgeClient()
    monkeypatch.setattr(session_mod, "resolve", lambda spec: _bridge_session())
    monkeypatch.setattr(bridge.BridgeControlClient, "from_runtime", classmethod(lambda cls: fake))
    monkeypatch.setattr(tmux, "kill_session", lambda *args, **kwargs: pytest.fail("tmux.kill_session should not run"))

    result = CliRunner().invoke(app, ["kill", "winbox/task"])

    assert result.exit_code == EXIT_OK
    assert fake.killed == [{"remote": "winbox", "name": "task"}]
