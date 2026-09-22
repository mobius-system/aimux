from __future__ import annotations

from collections import deque
import json
import io
import os
import subprocess
import threading
import time

import pytest
from loguru import logger

from aimux import __version__
from aimux import bridge
from aimux import bridge_server
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


def test_windows_pipe_adapter_never_allocates_console(monkeypatch):
    """The v2 Windows profile must use pipes + CREATE_NO_WINDOW end-to-end."""
    from aimux import bridge_client

    class FakeStdout:
        def __init__(self):
            self.chunks = iter([b"ready\r\n", b""])

        def read1(self, _size):
            return next(self.chunks)

    class FakeStdin(io.BytesIO):
        def flush(self):
            pass

    class FakeProcess:
        pid = 4242

        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()
            self._returncode = None

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            self._returncode = 0
            return 0

        def terminate(self):
            self._returncode = -15

        def kill(self):
            self._returncode = -9

    calls = {}
    process = FakeProcess()

    def fake_popen(command, **kwargs):
        calls["command"] = command
        calls.update(kwargs)
        return process

    monkeypatch.setattr(bridge_client.sys, "platform", "win32")
    monkeypatch.setattr(bridge_client.subprocess, "Popen", fake_popen)
    adapter = bridge_client.WindowsPipeAdapter("cmd", "cmd.exe")
    session = adapter.create(name="hidden", cmd=None, cwd=None, created_at="now")
    thread = session.metadata["output_thread"]
    thread.join(timeout=1)

    assert calls["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    assert calls["stdin"] is subprocess.PIPE
    assert calls["stdout"] is subprocess.PIPE
    assert calls["stderr"] is subprocess.STDOUT
    adapter.send_keys(session, ["echo ready", "Enter"], [])
    assert process.stdin.getvalue() == b"echo ready\r\n"
    assert adapter.capture(session, 10, ansi=False).content == "ready\n"


def test_windows_pipe_adapter_encodes_chinese_for_console_code_page(monkeypatch):
    from aimux import bridge_client

    class FakeStdin(io.BytesIO):
        def flush(self):
            pass

    class FakeProcess:
        stdin = FakeStdin()

    adapter = bridge_client.WindowsPipeAdapter("powershell", "powershell.exe")
    session = bridge_client.ClientSession(
        name="chinese-input",
        profile="powershell",
        command="powershell.exe",
        cwd=None,
        created_at="now",
        process=FakeProcess(),
        adapter=adapter,
    )
    monkeypatch.setattr(adapter, "_stdin_encoding", lambda: "cp936")

    adapter.send_keys(session, ["python.exe test.py 中文测试", "Enter"], [])

    assert session.process.stdin.getvalue() == "python.exe test.py 中文测试\r\n".encode("cp936")
    assert "中文测试".encode("utf-8") not in session.process.stdin.getvalue()


def test_windows_pipe_adapter_keeps_utf8_for_mingw_and_pwsh():
    from aimux import bridge_client

    assert bridge_client.WindowsPipeAdapter("mingw64", "bash.exe -i")._stdin_encoding() == "utf-8"
    assert bridge_client.WindowsPipeAdapter("pwsh", "pwsh.exe")._stdin_encoding() == "utf-8"


def test_windows_pipe_output_falls_back_to_console_code_page():
    from aimux import bridge_client

    session = bridge_client.ClientSession(
        name="native-output",
        profile="cmd",
        command="cmd.exe /Q",
        cwd=None,
        created_at="now",
        metadata={
            "output": deque(),
            "output_chars": 0,
            "output_lock": threading.Lock(),
            "output_fallback_encoding": "cp936",
        },
    )

    bridge_client.WindowsPipeAdapter._append_output(session, "版权所有".encode("cp936"))
    bridge_client.WindowsPipeAdapter._append_output(session, "中文测试".encode("utf-8"))

    assert "".join(session.metadata["output"]) == "版权所有中文测试"


def test_windows_pipe_adapter_reports_unencodable_input(monkeypatch):
    from aimux import bridge_client

    class FakeStdin(io.BytesIO):
        def flush(self):
            pass

    class FakeProcess:
        stdin = FakeStdin()

    adapter = bridge_client.WindowsPipeAdapter("cmd", "cmd.exe")
    session = bridge_client.ClientSession(
        name="emoji-input",
        profile="cmd",
        command="cmd.exe",
        cwd=None,
        created_at="now",
        process=FakeProcess(),
        adapter=adapter,
    )
    monkeypatch.setattr(adapter, "_stdin_encoding", lambda: "cp936")

    with pytest.raises(bridge.BridgeError, match=r"cannot be encoded as cp936") as exc_info:
        adapter.send_keys(session, ["echo 🚀", "Enter"], [])

    assert exc_info.value.kind == "encoding_error"
    assert session.process.stdin.getvalue() == b""


def test_build_adapters_v2_keeps_legacy_profile_names(monkeypatch):
    from aimux import bridge_client

    monkeypatch.setattr(bridge_client.sys, "platform", "win32")
    monkeypatch.setattr(bridge_client.shutil, "which", lambda _name: None)
    monkeypatch.setattr(bridge_client, "find_git_bash", lambda: None)
    adapters, default = bridge_client.build_adapters(silent_shell_v2=True)

    assert default == "cmd"
    assert set(adapters) == {"cmd", "powershell"}
    assert all(isinstance(adapter, bridge_client.WindowsPipeAdapter) for adapter in adapters.values())
    assert all(adapter.profile_info()["visible"] is False for adapter in adapters.values())
    assert adapters["cmd"].command == "cmd.exe /D /Q"
    assert adapters["powershell"].command.endswith("-Command -")


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX PTY")
def test_posix_pty_default_shell_stays_alive_and_accepts_input(tmp_path):
    from aimux import bridge_client

    adapter = bridge_client.PosixPtyAdapter("posix-pty", command="/bin/sh")
    session = adapter.create(name="pty-alive", cmd=None, cwd=str(tmp_path), created_at="now")
    try:
        time.sleep(0.1)
        assert session.process is not None
        assert session.process.poll() is None
        adapter.send_keys(session, ["printf AIMUX_PTY_OK", "Enter"], [])
        deadline = time.monotonic() + 2
        content = ""
        while time.monotonic() < deadline:
            content = adapter.capture(session, 20, ansi=False).content
            if "AIMUX_PTY_OK" in content:
                break
            time.sleep(0.05)
        assert "AIMUX_PTY_OK" in content
    finally:
        adapter.kill(session)


def test_runtime_reports_signal_for_crashed_session(monkeypatch):
    from aimux import bridge_client

    class CrashedProcess:
        returncode = -11

        @staticmethod
        def poll():
            return -11

    runtime = bridge_client.ReverseClientRuntime("test")
    runtime.sessions["crashed"] = bridge_client.ClientSession(
        name="crashed",
        profile="posix-pty",
        command="/bin/sh",
        cwd=None,
        created_at="now",
        process=CrashedProcess(),
    )

    with pytest.raises(bridge.BridgeError, match=r"returncode=-11, signal=SIGSEGV"):
        runtime._get("crashed")


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
            "client_version": "",
            "allow_http_ports": [],
            "server_version": __version__,
            "version_mismatch": False,
        }
    ]
    assert state.client_supports_profile("winbox", "cmd") is True
    assert state.client_supports_profile("winbox", "powershell") is False


def test_bridge_server_prints_version_on_startup(monkeypatch):
    calls: dict[str, object] = {}
    logs: list[str] = []
    sink_id = logger.add(lambda message: logs.append(str(message)), format="{message}")

    class FakeHTTPServer:
        def __init__(self, address, handler):
            calls["address"] = address
            calls["handler"] = handler
            self.server_address = address

        def serve_forever(self):
            calls["served"] = True

        def server_close(self):
            calls["closed"] = True

    monkeypatch.setattr(bridge_server, "ThreadingHTTPServer", FakeHTTPServer)
    monkeypatch.setattr(bridge_server, "write_runtime", lambda runtime: calls.setdefault("runtime", runtime))
    monkeypatch.setattr(bridge_server, "clear_runtime", lambda: calls.setdefault("cleared", True))

    try:
        bridge_server.serve("127.0.0.1", 8765, token="secret")
    finally:
        logger.remove(sink_id)

    assert any(f"aimux bridge server version {__version__}" in line for line in logs)
    assert any("bridge listening on 127.0.0.1:8765" in line for line in logs)
    assert calls["served"] is True
    assert calls["closed"] is True
    assert calls["cleared"] is True


def test_register_payload_advertises_client_version():
    """The reverse client advertises its aimux version when registering so the
    broker can detect client/server skew instead of failing silently."""
    from aimux import bridge_client

    runtime = bridge_client.ReverseClientRuntime("winbox_1")
    payload = runtime.registration_payload(replace=False)
    assert payload["version"] == __version__
    assert payload["client_id"] == runtime.client_id


def test_register_client_flags_version_mismatch(monkeypatch):
    monkeypatch.setattr("aimux.bridge_server.ssh_mod.host_exists", lambda name: False)
    state = BridgeBrokerState("secret")

    result = state.register_client(
        {
            "identifier": "winbox_1",
            "platform": "linux",
            "profiles": [],
            "version": "0.0.1-ancient",
        }
    )

    assert result["ok"] is True
    assert result["server_version"] == __version__
    assert result["version_mismatch"] is True
    assert state.clients["winbox_1"].client_version == "0.0.1-ancient"
    remote = state.list_remotes()[0]
    assert remote["client_version"] == "0.0.1-ancient"
    assert remote["server_version"] == __version__
    assert remote["version_mismatch"] is True


def test_register_client_silent_when_versions_match(monkeypatch):
    monkeypatch.setattr("aimux.bridge_server.ssh_mod.host_exists", lambda name: False)
    state = BridgeBrokerState("secret")

    result = state.register_client(
        {
            "identifier": "winbox_1",
            "platform": "linux",
            "profiles": [],
            "version": __version__,
        }
    )

    assert result["version_mismatch"] is False
    assert state.list_remotes()[0]["version_mismatch"] is False


def test_register_client_tolerates_legacy_client_without_version(monkeypatch):
    """An older client that sends no version must not false-alarm — only a
    known-and-different version triggers a mismatch."""
    monkeypatch.setattr("aimux.bridge_server.ssh_mod.host_exists", lambda name: False)
    state = BridgeBrokerState("secret")

    result = state.register_client(
        {"identifier": "winbox_1", "platform": "linux", "profiles": []}
    )

    assert result["version_mismatch"] is False
    assert state.clients["winbox_1"].client_version == ""
    assert state.list_remotes()[0]["client_version"] == ""


def test_register_client_emits_warning_log_on_mismatch(monkeypatch):
    """User-facing requirement: a version skew must produce a WARNING log so
    operators see it without polling the API."""
    monkeypatch.setattr("aimux.bridge_server.ssh_mod.host_exists", lambda name: False)
    logs: list[str] = []
    sink_id = logger.add(lambda message: logs.append(str(message)), level="WARNING", format="{message}")
    try:
        state = BridgeBrokerState("secret")
        state.register_client(
            {
                "identifier": "winbox_1",
                "platform": "linux",
                "profiles": [],
                "version": "0.0.1-ancient",
            }
        )
    finally:
        logger.remove(sink_id)

    joined = "\n".join(logs)
    assert "version mismatch" in joined
    assert "0.0.1-ancient" in joined
    assert __version__ in joined


def test_register_client_preserves_sessions_for_same_client_id(monkeypatch):
    monkeypatch.setattr("aimux.bridge_server.ssh_mod.host_exists", lambda name: False)
    state = BridgeBrokerState("secret")
    state.register_client(
        {
            "identifier": "winbox_1",
            "platform": "linux",
            "profiles": [{"name": "posix-pty", "available": True}],
            "client_id": "client-a",
        }
    )
    state.sessions[("winbox_1", "shell")] = {
        "id": "winbox_1/shell",
        "location": "winbox_1",
        "name": "shell",
        "backend": "bridge",
    }

    state.register_client(
        {
            "identifier": "winbox_1",
            "platform": "linux",
            "profiles": [{"name": "posix-pty", "available": True}],
            "client_id": "client-a",
        }
    )

    assert ("winbox_1", "shell") in state.sessions


def test_register_client_clears_sessions_for_new_client_id(monkeypatch):
    monkeypatch.setattr("aimux.bridge_server.ssh_mod.host_exists", lambda name: False)
    state = BridgeBrokerState("secret")
    state.register_client(
        {
            "identifier": "winbox_1",
            "platform": "linux",
            "profiles": [{"name": "posix-pty", "available": True}],
            "client_id": "client-a",
        }
    )
    state.sessions[("winbox_1", "shell")] = {
        "id": "winbox_1/shell",
        "location": "winbox_1",
        "name": "shell",
        "backend": "bridge",
    }

    state.register_client(
        {
            "identifier": "winbox_1",
            "platform": "linux",
            "profiles": [{"name": "posix-pty", "available": True}],
            "client_id": "client-b",
        }
    )

    assert ("winbox_1", "shell") not in state.sessions


def test_disconnect_events_fails_pending_requests_without_waiting_for_timeout(monkeypatch):
    monkeypatch.setattr("aimux.bridge_server.ssh_mod.host_exists", lambda name: False)
    state = BridgeBrokerState("secret")
    state.register_client(
        {
            "identifier": "winbox_1",
            "platform": "linux",
            "profiles": [{"name": "posix-pty", "available": True}],
            "client_id": "client-a",
        }
    )
    q = state.connect_events("winbox_1")
    queued = threading.Event()
    result: dict[str, object] = {}

    def submitter() -> None:
        queued.set()
        try:
            state.submit("winbox_1", "session.capture", {"session": "shell"}, timeout=30.0)
        except bridge.BridgeError as exc:
            result["kind"] = exc.kind
            result["exit_code"] = exc.exit_code
            result["message"] = exc.message

    thread = threading.Thread(target=submitter)
    thread.start()
    assert queued.wait(timeout=1.0)
    event = q.get(timeout=1.0)
    assert event is not None
    assert event["type"] == "session.capture"

    state.disconnect_events("winbox_1", q)
    thread.join(timeout=1.0)

    assert not thread.is_alive()
    assert result["kind"] == "remote_disconnected"
    assert result["exit_code"] == 2
    assert "session.capture" in str(result["message"])
