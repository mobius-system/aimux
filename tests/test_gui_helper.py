"""Unit tests for the GUI helper manager and the gui.request bridge path."""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import pytest

from aimux import gui_helper
from aimux.bridge import BridgeError
from aimux.bridge_client import ReverseClientRuntime
from aimux.bridge_server import BridgeBrokerState

# A stand-in for the Rust helper: same stdin/stdout JSON-lines protocol.
MOCK_HELPER = r"""
import json, os, sys
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    cmd = req.get("cmd")
    if cmd == "diagnostics":
        result = {"protocolVersion": 4, "os": "windows", "arch": "x86_64", "pid": os.getpid()}
        resp = {"protocolVersion": 4, "id": req.get("id"), "ok": True, "result": result}
    elif cmd == "listRoots":
        resp = {"protocolVersion": 4, "id": req.get("id"), "ok": True,
                "result": {"roots": [{"rootRef": "@w1", "title": "mock", "pid": 1}]}}
    elif cmd == "boom":
        resp = {"protocolVersion": 4, "id": req.get("id"), "ok": False,
                "error": {"message": "mock failure", "code": "target_not_found"}}
    else:
        resp = {"protocolVersion": 4, "id": req.get("id"), "ok": True, "result": {"echo": cmd}}
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"""


@pytest.fixture()
def mock_helper_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "mock-helper"
    script.write_text("#!/usr/bin/env python3\n" + MOCK_HELPER, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("AIMUX_GUI_HELPER_PATH", str(script))
    return script


def test_platform_key_none_on_linux() -> None:
    if sys.platform.startswith("win"):
        pytest.skip("linux-only expectation")
    assert gui_helper._platform_key() is None


def test_install_rejects_unsupported_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIMUX_GUI_HELPER_PATH", raising=False)
    if gui_helper._platform_key() is not None:
        pytest.skip("runs on supported platform")
    with pytest.raises(gui_helper.GuiHelperError, match="only available for Windows"):
        gui_helper.install()


def test_start_probe_and_roundtrip(mock_helper_path: Path) -> None:
    helper, status = gui_helper.start()
    assert helper is not None
    try:
        assert status["available"] is True
        assert status["protocolVersion"] == 4
        assert helper.alive()
        res = helper.request("listRoots", {})
        assert res["ok"] is True
        assert res["result"]["roots"][0]["rootRef"] == "@w1"
    finally:
        helper.stop()
    assert not helper.alive()


def test_request_after_death_reports_not_running(mock_helper_path: Path) -> None:
    helper, _ = gui_helper.start()
    assert helper is not None
    helper.stop()
    with pytest.raises(gui_helper.GuiHelperError, match="not running"):
        helper.request("listRoots", {})


def test_runtime_gui_request_maps_helper_error(mock_helper_path: Path) -> None:
    runtime = ReverseClientRuntime("unit-gui", enable_gui=True)
    assert runtime.gui_helper is not None
    assert runtime.registration_payload(replace=False)["gui"]["available"] is True
    with pytest.raises(BridgeError) as excinfo:
        runtime.handle("gui.request", {"cmd": "boom", "args": {}})
    assert "mock failure" in str(excinfo.value)
    runtime.shutdown()


def test_runtime_without_gui_reports_unavailable() -> None:
    runtime = ReverseClientRuntime("unit-nogui")
    payload = runtime.registration_payload(replace=False)
    assert payload["gui"]["available"] is False
    with pytest.raises(BridgeError, match="--enable-gui"):
        runtime.handle("gui.request", {"cmd": "diagnostics", "args": {}})


def test_broker_registry_stores_and_lists_gui() -> None:
    state = BridgeBrokerState(token="t")
    state.register_client(
        {
            "identifier": "win-box",
            "platform": "windows",
            "default_profile": "cmd",
            "profiles": [],
            "gui": {"available": True, "protocolVersion": 4},
        }
    )
    state.register_client(
        {"identifier": "old-box", "platform": "windows", "default_profile": "cmd", "profiles": []}
    )
    rows = {row["name"]: row for row in state.list_remotes()}
    assert rows["win-box"]["gui"] == {"available": True, "protocolVersion": 4}
    assert rows["old-box"]["gui"] is None  # old clients: absent, not False


def test_broker_routes_gui_request_to_client_queue() -> None:
    state = BridgeBrokerState(token="t")
    state.register_client({"identifier": "win-box", "platform": "windows", "default_profile": "cmd", "profiles": []})
    q = state.connect_events("win-box")

    result: dict = {}
    done = threading.Event()

    def responder() -> None:
        item = q.get(timeout=5)
        result["event"] = item
        state.complete({"id": item["id"], "ok": True, "payload": {"lookId": "look_1"}})
        done.set()

    threading.Thread(target=responder, daemon=True).start()
    payload = state.submit("win-box", "gui.request", {"cmd": "look", "args": {}}, timeout=5.0)
    assert done.wait(5)
    assert payload == {"lookId": "look_1"}
    assert result["event"]["type"] == "gui.request"
