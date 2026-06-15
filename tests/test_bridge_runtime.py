from __future__ import annotations

import json

import pytest

from aimux import bridge
from aimux.bridge_client import _keys_to_text
from aimux.bridge_server import BridgeBrokerState


def test_bridge_runtime_round_trip(tmp_path, monkeypatch):
    path = tmp_path / "runtime.json"
    monkeypatch.setenv(bridge.RUNTIME_ENV, str(path))

    runtime = bridge.BridgeRuntime(
        url="http://127.0.0.1:8765",
        token="secret",
        pid=123,
        host="0.0.0.0",
        port=8765,
        created_at="2026-06-15T00:00:00Z",
    )
    bridge.write_runtime(runtime)

    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "secret"
    assert bridge.read_runtime() == runtime

    bridge.clear_runtime()
    assert bridge.read_runtime() is None


def test_bridge_identifier_validation_rejects_ambiguous_names():
    for value in ["", "local", "bad/name", "bad name"]:
        with pytest.raises(bridge.BridgeError):
            bridge.validate_identifier(value)

    bridge.validate_identifier("winbox_1.prod")


def test_bridge_key_translation_supports_common_keys():
    assert _keys_to_text(["dir", "Enter"], []) == "dir\r"
    assert _keys_to_text(["x"], ["-N", "3"]) == "xxx"
    assert _keys_to_text(["Enter"], ["-l"]) == "Enter"

    with pytest.raises(bridge.BridgeError):
        _keys_to_text(["x"], ["-X"])


def test_broker_state_registers_bridge_remote(monkeypatch):
    monkeypatch.setattr("aimux.bridge_server.ssh_mod.host_exists", lambda name: False)
    state = BridgeBrokerState("secret")

    state.register_client(
        {
            "identifier": "winbox",
            "platform": "windows",
            "default_profile": "cmd",
            "profiles": [{"name": "cmd", "available": True}],
        }
    )

    assert state.list_remotes() == [
        {
            "name": "winbox",
            "type": "bridge",
            "status": "disconnected",
            "platform": "windows",
            "default_profile": "cmd",
            "profiles": [{"name": "cmd", "available": True}],
            "last_seen": state.list_remotes()[0]["last_seen"],
        }
    ]
    assert state.client_supports_profile("winbox", "cmd") is True
    assert state.client_supports_profile("winbox", "powershell") is False
