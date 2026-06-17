"""Tests for the bridge file.* message family and the bridge_send_files / bridge_get_files transfer paths."""
from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import pytest

from aimux import transfer
from aimux.bridge_client import ReverseClientRuntime
from aimux.bridge_server import BridgeBrokerState


# ---------------------------------------------------------------------------
# Client-side handlers (ReverseClientRuntime.handle file.* dispatch)
# ---------------------------------------------------------------------------


def _runtime() -> ReverseClientRuntime:
    return ReverseClientRuntime("test-remote")


def test_file_stat_reports_missing_file_as_none(tmp_path):
    runtime = _runtime()
    result = runtime.handle("file.stat", {"path": str(tmp_path / "missing")})
    assert result == {"exists": False, "type": "none", "size": 0}


def test_file_stat_identifies_file_and_dir(tmp_path):
    runtime = _runtime()
    file_path = tmp_path / "data.bin"
    file_path.write_bytes(b"hello")
    sub = tmp_path / "sub"
    sub.mkdir()

    assert runtime.handle("file.stat", {"path": str(file_path)}) == {
        "exists": True,
        "type": "file",
        "size": 5,
    }
    assert runtime.handle("file.stat", {"path": str(sub)}) == {
        "exists": True,
        "type": "dir",
        "size": 0,
    }


def test_file_write_and_read_round_trip_binary(tmp_path):
    runtime = _runtime()
    target = tmp_path / "deep" / "payload.bin"
    payload = bytes(range(256)) + b"\xff\x00\xfe"

    written = runtime.handle(
        "file.write",
        {"path": str(target), "content_b64": base64.b64encode(payload).decode("ascii"), "mode": 0o600},
    )
    assert written == {"size": len(payload)}
    assert target.read_bytes() == payload
    assert (target.stat().st_mode & 0o777) == 0o600

    read = runtime.handle("file.read", {"path": str(target)})
    assert read["size"] == len(payload)
    assert base64.b64decode(read["content_b64"]) == payload


def test_file_mkdir_is_idempotent(tmp_path):
    runtime = _runtime()
    target = tmp_path / "a" / "b" / "c"
    runtime.handle("file.mkdir", {"path": str(target)})
    runtime.handle("file.mkdir", {"path": str(target)})
    assert target.is_dir()


def test_file_list_walks_directory_recursively(tmp_path):
    runtime = _runtime()
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "1.txt").write_text("a")
    (tmp_path / "x" / "sub").mkdir()
    (tmp_path / "x" / "sub" / "2.txt").write_text("bb")

    result = runtime.handle("file.list", {"path": str(tmp_path / "x"), "recursive": True})
    entries = {(e["path"], e["type"], e["size"]) for e in result["entries"]}
    assert (str(tmp_path / "x" / "1.txt"), "file", 1) in entries
    assert (str(tmp_path / "x" / "sub" / "2.txt"), "file", 2) in entries
    assert (str(tmp_path / "x" / "sub"), "dir", 0) in entries


def test_file_list_non_recursive_returns_only_immediate_children(tmp_path):
    runtime = _runtime()
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "1.txt").write_text("a")
    (tmp_path / "x" / "sub").mkdir()
    (tmp_path / "x" / "sub" / "deep.txt").write_text("deep")

    result = runtime.handle("file.list", {"path": str(tmp_path / "x"), "recursive": False})
    types = {e["type"] for e in result["entries"]}
    paths = {e["path"] for e in result["entries"]}
    assert types == {"file", "dir"}
    assert str(tmp_path / "x" / "sub" / "deep.txt") not in paths


def test_file_handlers_reject_paths_with_control_chars():
    runtime = _runtime()
    for action, payload in [
        ("file.stat", {"path": "ab\0cd"}),
        ("file.read", {"path": "ab\ncd"}),
        ("file.write", {"path": "ab\rcd", "content_b64": ""}),
        ("file.mkdir", {"path": "ab\ncd"}),
        ("file.list", {"path": "ab\0cd"}),
    ]:
        with pytest.raises(Exception) as exc:
            runtime.handle(action, payload)
        assert "control" in str(exc.value).lower() or "bad_request" in str(exc.value).lower()


def test_file_write_enforces_max_size(tmp_path):
    runtime = _runtime()
    payload = b"x" * 100
    with pytest.raises(Exception) as exc:
        runtime.handle(
            "file.write",
            {
                "path": str(tmp_path / "big"),
                "content_b64": base64.b64encode(payload).decode("ascii"),
                "max_size": 10,
            },
        )
    assert "exceeds" in str(exc.value).lower() or "too_large" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Broker routing
# ---------------------------------------------------------------------------


class _CollectingBrokerState(BridgeBrokerState):
    """Captures submitted file.* payloads for inspection without a real remote."""

    def __init__(self, token: str = "tok"):
        super().__init__(token)
        self.submitted: list[tuple[str, str, dict[str, Any]]] = []

    def submit(self, remote: str, message_type: str, payload: dict[str, Any], timeout: float):
        # Record the call; return a canned response keyed by message type.
        self.submitted.append((remote, message_type, payload))
        if message_type == "file.stat":
            return {"exists": True, "type": "file", "size": payload.get("__size", 7)}
        if message_type == "file.read":
            return {"content_b64": "AAA=", "size": 2}
        if message_type == "file.write":
            return {"size": len(base64.b64decode(payload.get("content_b64", "")))}
        if message_type == "file.mkdir":
            return {"ok": True}
        if message_type == "file.list":
            return {"entries": []}
        raise AssertionError(f"unexpected message type {message_type!r}")


def test_broker_file_op_dispatches_to_submit_with_correct_message_type():
    state = _CollectingBrokerState()
    state.file_op("remote-a", "stat", {"path": "/tmp/x"}, timeout=10)
    state.file_op("remote-a", "read", {"path": "/tmp/x"}, timeout=10)
    state.file_op("remote-a", "write", {"path": "/tmp/x", "content_b64": "aGVsbG8="}, timeout=10)
    state.file_op("remote-a", "mkdir", {"path": "/tmp/x"}, timeout=10)
    state.file_op("remote-a", "list", {"path": "/tmp/x"}, timeout=10)

    assert [(r, m) for r, m, _ in state.submitted] == [
        ("remote-a", "file.stat"),
        ("remote-a", "file.read"),
        ("remote-a", "file.write"),
        ("remote-a", "file.mkdir"),
        ("remote-a", "file.list"),
    ]


def test_broker_file_op_rejects_unknown_action():
    state = _CollectingBrokerState()
    with pytest.raises(Exception):
        state.file_op("remote-a", "delete", {"path": "/tmp/x"}, timeout=10)


# ---------------------------------------------------------------------------
# bridge_send_files / bridge_get_files (transfer module)
# ---------------------------------------------------------------------------


class FakeBridgeControlClient:
    """In-memory BridgeControlClient stub used by transfer tests."""

    def __init__(self):
        self.made_dirs: list[str] = []
        self.written: dict[str, bytes] = {}
        self.remote_files: dict[str, bytes] = {}
        self.remote_dirs: set[str] = set()
        self.stat_called: list[str] = []
        self.list_called: list[str] = []

    def file_mkdir(self, *, remote: str, path: str, timeout: float = 60.0):
        self.made_dirs.append(path)
        self.remote_dirs.add(path)
        return {"ok": True}

    def file_write(self, *, remote: str, path: str, content_b64: str, mode=None, max_size=None, timeout: float = 180.0):
        data = base64.b64decode(content_b64)
        if max_size and len(data) > max_size:
            raise transfer.TransferError(
                f"file exceeds bridge size limit ({len(data)} > {max_size} bytes): {path}"
            )
        self.written[path] = data
        return {"size": len(data)}

    def file_stat(self, *, remote: str, path: str, timeout: float = 60.0):
        self.stat_called.append(path)
        if path in self.remote_files:
            return {"exists": True, "type": "file", "size": len(self.remote_files[path])}
        if path in self.remote_dirs:
            return {"exists": True, "type": "dir", "size": 0}
        return {"exists": False, "type": "none", "size": 0}

    def file_read(self, *, remote: str, path: str, timeout: float = 180.0):
        return {
            "content_b64": base64.b64encode(self.remote_files[path]).decode("ascii"),
            "size": len(self.remote_files[path]),
        }

    def file_list(self, *, remote: str, path: str, recursive: bool = True, timeout: float = 60.0):
        self.list_called.append(path)
        entries: list[dict[str, Any]] = []
        prefix = path.rstrip("/") + "/"
        for key, value in self.remote_files.items():
            if key.startswith(prefix):
                entries.append({"path": key, "type": "file", "size": len(value)})
        for d in self.remote_dirs:
            if d.startswith(prefix):
                entries.append({"path": d, "type": "dir", "size": 0})
        return {"entries": entries}


def test_bridge_send_files_uploads_single_file(tmp_path):
    local_file = tmp_path / "src.txt"
    local_file.write_bytes(b"hello world")

    fake = FakeBridgeControlClient()
    result = transfer.bridge_send_files(fake, "remote-1", [str(local_file)], "/remote/out")

    assert result.uploaded_files == 1
    assert fake.written == {"/remote/out/src.txt": b"hello world"}
    assert "/remote/out" in fake.made_dirs


def test_bridge_send_files_uploads_directory_recursively(tmp_path):
    root = tmp_path / "proj"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a")
    (root / "sub" / "b.txt").write_text("bb")

    fake = FakeBridgeControlClient()
    result = transfer.bridge_send_files(fake, "remote-1", [str(root)], "/remote/out")

    assert result.uploaded_files == 2
    written_keys = set(fake.written.keys())
    assert "/remote/out/proj/a.txt" in written_keys
    assert "/remote/out/proj/sub/b.txt" in written_keys
    # parent dirs created
    assert "/remote/out/proj" in fake.made_dirs
    assert "/remote/out/proj/sub" in fake.made_dirs


def test_bridge_send_files_rejects_oversized_file(tmp_path):
    local_file = tmp_path / "big.bin"
    local_file.write_bytes(b"x" * 1024)

    fake = FakeBridgeControlClient()
    with pytest.raises(transfer.TransferError) as exc:
        transfer.bridge_send_files(
            fake, "remote-1", [str(local_file)], "/remote/out", max_file_size=100
        )
    assert "exceeds bridge size limit" in str(exc.value)
    assert fake.written == {}


def test_bridge_send_files_rejects_path_traversal_in_remote_target(tmp_path):
    local_file = tmp_path / "ok.txt"
    local_file.write_text("ok")

    fake = FakeBridgeControlClient()
    with pytest.raises(transfer.TransferError):
        transfer.bridge_send_files(fake, "remote-1", [str(local_file)], "/remote/../etc/passwd")
    assert fake.written == {}


def test_bridge_get_files_downloads_single_file(tmp_path):
    fake = FakeBridgeControlClient()
    fake.remote_files["/remote/log.txt"] = b"log line\n"

    dest = tmp_path / "downloads"
    result = transfer.bridge_get_files(fake, "remote-1", ["/remote/log.txt"], dest)

    assert result.downloaded_paths == 1
    assert (dest / "log.txt").read_bytes() == b"log line\n"


def test_bridge_get_files_downloads_directory_recursively(tmp_path):
    fake = FakeBridgeControlClient()
    fake.remote_dirs.add("/remote/logs")
    fake.remote_files["/remote/logs/a.txt"] = b"aaa"
    fake.remote_files["/remote/logs/sub/b.txt"] = b"bbb"

    dest = tmp_path / "downloads"
    result = transfer.bridge_get_files(fake, "remote-1", ["/remote/logs"], dest)

    assert result.downloaded_paths == 2
    assert (dest / "logs" / "a.txt").read_bytes() == b"aaa"
    assert (dest / "logs" / "sub" / "b.txt").read_bytes() == b"bbb"


def test_bridge_get_files_raises_when_remote_path_missing(tmp_path):
    fake = FakeBridgeControlClient()
    dest = tmp_path / "downloads"

    with pytest.raises(transfer.TransferError) as exc:
        transfer.bridge_get_files(fake, "remote-1", ["/no/such/path"], dest)
    assert "does not exist" in str(exc.value)


def test_bridge_get_files_enforces_size_limit(tmp_path):
    fake = FakeBridgeControlClient()
    fake.remote_files["/remote/huge.bin"] = b"x" * 1024

    dest = tmp_path / "downloads"
    with pytest.raises(transfer.TransferError):
        transfer.bridge_get_files(fake, "remote-1", ["/remote/huge.bin"], dest, max_file_size=100)


# ---------------------------------------------------------------------------
# CLI dispatch (send_files / get_files route to bridge remote when applicable)
# ---------------------------------------------------------------------------

pytest.importorskip("typer")
from typer.testing import CliRunner  # noqa: E402

from aimux.cli import app  # noqa: E402
from aimux.constants import EXIT_OK, EXIT_USER_ERROR  # noqa: E402


class CapturingFakeBridgeClient(FakeBridgeControlClient):
    """Fake client that also satisfies the minimal interface the CLI uses."""

    pass


def _patch_bridge_remote(monkeypatch, fake):
    def fake_bridge_remote(name):
        return fake, {"name": name, "status": "connected"}
    monkeypatch.setattr("aimux.cli._bridge_remote", fake_bridge_remote)


def test_cli_send_files_dispatches_to_bridge(monkeypatch, tmp_path):
    fake = CapturingFakeBridgeClient()
    _patch_bridge_remote(monkeypatch, fake)
    monkeypatch.setattr("aimux.cli.ssh_mod.host_exists", lambda name: False)

    local = tmp_path / "a.txt"
    local.write_text("hello")

    result = CliRunner().invoke(app, ["send_files", "remote-1", "/remote/out", str(local)])

    assert result.exit_code == EXIT_OK, result.output
    assert "uploaded 1 file(s) to bridge remote remote-1:/remote/out" in result.output
    assert fake.written == {"/remote/out/a.txt": b"hello"}


def test_cli_get_files_dispatches_to_bridge(monkeypatch, tmp_path):
    fake = CapturingFakeBridgeClient()
    fake.remote_files["/remote/data.txt"] = b"payload"
    _patch_bridge_remote(monkeypatch, fake)
    monkeypatch.setattr("aimux.cli.ssh_mod.host_exists", lambda name: False)

    dest = tmp_path / "downloads"
    result = CliRunner().invoke(app, ["get_files", "remote-1", str(dest), "/remote/data.txt"])

    assert result.exit_code == EXIT_OK, result.output
    assert "downloaded 1 path(s) from bridge remote remote-1" in result.output
    assert (dest / "data.txt").read_bytes() == b"payload"


def test_cli_send_files_falls_through_to_ssh_error_when_no_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr("aimux.cli._bridge_remote", lambda name: (None, None))
    monkeypatch.setattr("aimux.cli.ssh_mod.host_exists", lambda name: False)

    result = CliRunner().invoke(app, ["send_files", "unknown-remote", "/out", str(tmp_path / "x")])

    assert result.exit_code == EXIT_USER_ERROR
    assert "not found in ~/.ssh/config or bridge registry" in result.output
