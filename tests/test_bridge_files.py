"""Tests for the bridge file.* message family and the bridge_send_files / bridge_get_files transfer paths."""
from __future__ import annotations

import base64
import hashlib
import io
import os
import tarfile
import threading
from pathlib import Path
from typing import Any

import pytest

from aimux import bridge
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


def test_file_archive_and_apply_archive_round_trip(tmp_path):
    runtime = _runtime()
    source = tmp_path / "source"
    source.mkdir()
    (source / "keep.txt").write_text("new", encoding="utf-8")
    (source / "empty").mkdir()
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "stale.txt").write_text("old", encoding="utf-8")

    archive = runtime.handle(
        "file.archive",
        {"path": str(source), "rel_paths": ["keep.txt"], "dirs": ["empty"]},
    )
    result = runtime.handle(
        "file.apply_archive",
        {
            "path": str(destination),
            "archive_b64": archive["archive_b64"],
            "delete_paths": ["stale.txt"],
        },
    )

    assert result["files"] == 1
    assert result["deleted"] == 1
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "new"
    assert (destination / "empty").is_dir()
    assert not (destination / "stale.txt").exists()


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


def test_broker_file_round_trip_through_reverse_client_runtime(tmp_path):
    state = BridgeBrokerState("tok")
    runtime = ReverseClientRuntime("remote-1")
    state.register_client(runtime.registration_payload(replace=False))
    q = state.connect_events("remote-1")

    def worker():
        while True:
            item = q.get()
            if item is None:
                break
            try:
                result = runtime.handle(str(item["type"]), item.get("payload") or {})
                response = {"id": item["id"], "ok": True, "payload": result}
            except bridge.BridgeError as e:
                response = {
                    "id": item["id"],
                    "ok": False,
                    "error": {"kind": e.kind, "message": e.message, "code": e.exit_code},
                }
            state.complete(response)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        target = tmp_path / "remote" / "payload.bin"
        payload = bytes(range(64))
        state.file_op("remote-1", "mkdir", {"path": str(target.parent)}, timeout=1)
        state.file_op(
            "remote-1",
            "write",
            {"path": str(target), "content_b64": base64.b64encode(payload).decode("ascii")},
            timeout=1,
        )
        read = state.file_op("remote-1", "read", {"path": str(target)}, timeout=1)
    finally:
        q.put(None)
        thread.join(timeout=1)
        state.disconnect_events("remote-1", q)

    assert base64.b64decode(read["content_b64"]) == payload
    assert target.read_bytes() == payload


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
        prefix = _remote_key(path).rstrip("/") + "/"
        for key, value in self.remote_files.items():
            if _remote_key(key).startswith(prefix):
                entries.append({
                    "path": key,
                    "type": "file",
                    "size": len(value),
                    "sha256": hashlib.sha256(value).hexdigest(),
                })
        for d in self.remote_dirs:
            if _remote_key(d).startswith(prefix):
                entries.append({"path": d, "type": "dir", "size": 0})
        return {"entries": entries}

    def file_archive(
        self,
        *,
        remote: str,
        path: str,
        rel_paths: list[str],
        dirs: list[str],
        max_size=None,
        timeout: float = 180.0,
    ):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for rel in sorted(d for d in dirs if d != "."):
                info = tarfile.TarInfo(rel)
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            for rel in sorted(rel_paths):
                key = _remote_join(path, rel)
                data = self.remote_files[key]
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        data = buf.getvalue()
        return {"archive_b64": base64.b64encode(data).decode("ascii"), "size": len(data)}

    def file_apply_archive(
        self,
        *,
        remote: str,
        path: str,
        archive_b64: str,
        delete_paths: list[str],
        max_size=None,
        timeout: float = 180.0,
    ):
        deleted = 0
        for rel in sorted(delete_paths, key=lambda p: (p.count("/"), p), reverse=True):
            key = _remote_join(path, rel)
            if key in self.remote_files:
                del self.remote_files[key]
                deleted += 1
            if key in self.remote_dirs:
                self.remote_dirs.remove(key)
                deleted += 1
            prefix = _remote_key(key).rstrip("/") + "/"
            for file_key in list(self.remote_files):
                if _remote_key(file_key).startswith(prefix):
                    del self.remote_files[file_key]
            for dir_key in list(self.remote_dirs):
                if _remote_key(dir_key).startswith(prefix):
                    self.remote_dirs.remove(dir_key)

        self.remote_dirs.add(path)
        files = 0
        dirs = 0
        data = base64.b64decode(archive_b64)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                key = _remote_join(path, member.name)
                if member.isdir():
                    self.remote_dirs.add(key)
                    dirs += 1
                elif member.isfile():
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    self.remote_files[key] = extracted.read()
                    files += 1
        return {"files": files, "dirs": dirs, "deleted": deleted}


def _remote_key(path: str) -> str:
    return path.replace("\\", "/")


def _remote_join(root: str, rel: str) -> str:
    return _remote_key(root).rstrip("/") + "/" + rel


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


def test_bridge_get_files_downloads_windows_style_file_basename(tmp_path):
    fake = FakeBridgeControlClient()
    remote_path = r"C:\Users\dev\proj\logs\app.log"
    fake.remote_files[remote_path] = b"log line\n"

    dest = tmp_path / "downloads"
    result = transfer.bridge_get_files(fake, "remote-1", [remote_path], dest)

    assert result.downloaded_paths == 1
    assert (dest / "app.log").read_bytes() == b"log line\n"


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


def test_bridge_get_files_downloads_windows_style_directory_recursively(tmp_path):
    fake = FakeBridgeControlClient()
    remote_dir = r"C:\Users\dev\proj\logs"
    fake.remote_dirs.add(remote_dir)
    fake.remote_files[r"C:\Users\dev\proj\logs\a.txt"] = b"aaa"
    fake.remote_files[r"C:\Users\dev\proj\logs\sub\b.txt"] = b"bbb"

    dest = tmp_path / "downloads"
    result = transfer.bridge_get_files(fake, "remote-1", [remote_dir], dest)

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


def test_bridge_sync_dir_to_remote_sends_changed_files_and_deletes_stale(tmp_path):
    local = tmp_path / "local"
    (local / "sub").mkdir(parents=True)
    (local / "same.txt").write_text("same", encoding="utf-8")
    (local / "changed.txt").write_text("new", encoding="utf-8")
    (local / "sub" / "child.txt").write_text("child", encoding="utf-8")

    fake = FakeBridgeControlClient()
    fake.remote_dirs.update({"/remote/proj", "/remote/proj/sub", "/remote/proj/old"})
    fake.remote_files["/remote/proj/same.txt"] = b"same"
    fake.remote_files["/remote/proj/changed.txt"] = b"old"
    fake.remote_files["/remote/proj/stale.txt"] = b"stale"

    result = transfer.bridge_sync_dir(
        fake,
        "remote-1",
        "/remote/proj",
        local,
        to_remote=True,
    )

    assert result.synced_files == 2
    assert result.deleted_paths == 2
    assert fake.remote_files["/remote/proj/same.txt"] == b"same"
    assert fake.remote_files["/remote/proj/changed.txt"] == b"new"
    assert fake.remote_files["/remote/proj/sub/child.txt"] == b"child"
    assert "/remote/proj/stale.txt" not in fake.remote_files
    assert "/remote/proj/old" not in fake.remote_dirs


def test_bridge_sync_dir_from_remote_downloads_archive_and_deletes_stale(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    (local / "same.txt").write_text("same", encoding="utf-8")
    (local / "changed.txt").write_text("old", encoding="utf-8")
    (local / "stale.txt").write_text("stale", encoding="utf-8")

    fake = FakeBridgeControlClient()
    fake.remote_dirs.update({"/remote/proj", "/remote/proj/empty"})
    fake.remote_files["/remote/proj/same.txt"] = b"same"
    fake.remote_files["/remote/proj/changed.txt"] = b"new"

    result = transfer.bridge_sync_dir(
        fake,
        "remote-1",
        "/remote/proj",
        local,
        from_remote=True,
    )

    assert result.synced_files == 1
    assert result.deleted_paths == 1
    assert (local / "same.txt").read_text(encoding="utf-8") == "same"
    assert (local / "changed.txt").read_text(encoding="utf-8") == "new"
    assert not (local / "stale.txt").exists()
    assert (local / "empty").is_dir()


def test_bridge_sync_dir_to_remote_excludes_and_protects_paths(tmp_path):
    local = tmp_path / "local"
    (local / "cache").mkdir(parents=True)
    (local / "keep.txt").write_text("keep", encoding="utf-8")
    (local / "secret.env").write_text("local secret", encoding="utf-8")
    (local / "cache" / "out.bin").write_text("local cache", encoding="utf-8")

    fake = FakeBridgeControlClient()
    fake.remote_dirs.update({"/remote/proj", "/remote/proj/cache"})
    fake.remote_files["/remote/proj/secret.env"] = b"remote secret"
    fake.remote_files["/remote/proj/cache/out.bin"] = b"remote cache"
    fake.remote_files["/remote/proj/stale.txt"] = b"stale"
    events: list[tuple[str, dict[str, Any]]] = []

    result = transfer.bridge_sync_dir(
        fake,
        "remote-1",
        "/remote/proj",
        local,
        to_remote=True,
        exclude=["*.env", "cache/"],
        progress=lambda phase, payload: events.append((phase, payload)),
    )

    assert result.synced_files == 1
    assert result.deleted_paths == 1
    assert fake.remote_files["/remote/proj/keep.txt"] == b"keep"
    assert fake.remote_files["/remote/proj/secret.env"] == b"remote secret"
    assert fake.remote_files["/remote/proj/cache/out.bin"] == b"remote cache"
    assert "/remote/proj/stale.txt" not in fake.remote_files
    phases = [phase for phase, _ in events]
    assert phases == [
        "scan_source",
        "scan_destination",
        "plan",
        "archive",
        "transfer",
        "apply",
        "done",
    ]
    assert events[2][1] == {"changed_files": 1, "deleted_paths": 1, "dirs": 0}


def test_bridge_sync_dir_to_remote_excludes_directory_descendants_without_trailing_slash(tmp_path):
    local = tmp_path / "local"
    node_modules = local / "frontend" / "node_modules"
    esbuild_bin = node_modules / "esbuild" / "bin"
    src = local / "frontend" / "src"
    esbuild_bin.mkdir(parents=True)
    src.mkdir(parents=True)
    (src / "app.js").write_text("console.log('keep')\n", encoding="utf-8")
    (esbuild_bin / "package.txt").write_text("exclude me", encoding="utf-8")
    try:
        (esbuild_bin / "esbuild").symlink_to("package.txt")
    except OSError:
        pass

    fake = FakeBridgeControlClient()
    fake.remote_dirs.add("/remote/proj")

    result = transfer.bridge_sync_dir(
        fake,
        "remote-1",
        "/remote/proj",
        local,
        to_remote=True,
        exclude=["frontend/node_modules"],
    )

    assert result.synced_files == 1
    assert fake.remote_files == {"/remote/proj/frontend/src/app.js": b"console.log('keep')\n"}
    assert "/remote/proj/frontend/node_modules/esbuild/bin/package.txt" not in fake.remote_files
    assert "/remote/proj/frontend/node_modules" not in fake.remote_dirs


def test_bridge_sync_dir_from_remote_excludes_and_protects_paths(tmp_path):
    local = tmp_path / "local"
    (local / "cache").mkdir(parents=True)
    (local / "secret.env").write_text("local secret", encoding="utf-8")
    (local / "cache" / "out.bin").write_text("local cache", encoding="utf-8")
    (local / "stale.txt").write_text("stale", encoding="utf-8")

    fake = FakeBridgeControlClient()
    fake.remote_dirs.update({"/remote/proj", "/remote/proj/cache"})
    fake.remote_files["/remote/proj/keep.txt"] = b"keep"
    fake.remote_files["/remote/proj/secret.env"] = b"remote secret"
    fake.remote_files["/remote/proj/cache/out.bin"] = b"remote cache"

    result = transfer.bridge_sync_dir(
        fake,
        "remote-1",
        "/remote/proj",
        local,
        from_remote=True,
        exclude=["*.env", "cache/"],
    )

    assert result.synced_files == 1
    assert result.deleted_paths == 1
    assert (local / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (local / "secret.env").read_text(encoding="utf-8") == "local secret"
    assert (local / "cache" / "out.bin").read_text(encoding="utf-8") == "local cache"
    assert not (local / "stale.txt").exists()


def test_bridge_sync_dir_from_remote_excludes_directory_descendants_without_trailing_slash(tmp_path):
    local = tmp_path / "local"
    fake = FakeBridgeControlClient()
    fake.remote_dirs.update({
        "/remote/proj",
        "/remote/proj/frontend",
        "/remote/proj/frontend/node_modules",
        "/remote/proj/frontend/node_modules/esbuild",
        "/remote/proj/frontend/node_modules/esbuild/bin",
        "/remote/proj/frontend/src",
    })
    fake.remote_files["/remote/proj/frontend/src/app.js"] = b"console.log('keep')\n"
    fake.remote_files["/remote/proj/frontend/node_modules/esbuild/bin/esbuild"] = b"exclude me"

    result = transfer.bridge_sync_dir(
        fake,
        "remote-1",
        "/remote/proj",
        local,
        from_remote=True,
        exclude=["frontend/node_modules"],
    )

    assert result.synced_files == 1
    assert (local / "frontend" / "src" / "app.js").read_bytes() == b"console.log('keep')\n"
    assert not (local / "frontend" / "node_modules").exists()


def test_bridge_sync_dir_noop_emits_progress_without_applying(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    (local / "same.txt").write_text("same", encoding="utf-8")

    fake = FakeBridgeControlClient()
    fake.remote_dirs.add("/remote/proj")
    fake.remote_files["/remote/proj/same.txt"] = b"same"

    def fail_apply_archive(**kwargs):
        pytest.fail("file_apply_archive should not run when sync_dir has no work")

    fake.file_apply_archive = fail_apply_archive  # type: ignore[method-assign]
    events: list[tuple[str, dict[str, Any]]] = []

    result = transfer.bridge_sync_dir(
        fake,
        "remote-1",
        "/remote/proj",
        local,
        to_remote=True,
        progress=lambda phase, payload: events.append((phase, payload)),
    )

    assert result.synced_files == 0
    assert result.deleted_paths == 0
    assert [phase for phase, _ in events] == [
        "scan_source",
        "scan_destination",
        "plan",
        "noop",
        "done",
    ]


def test_apply_sync_archive_validates_archive_before_deleting(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    stale = root / "stale.txt"
    stale.write_text("keep until archive is valid", encoding="utf-8")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("bad-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "elsewhere"
        tar.addfile(info)

    with pytest.raises(transfer.TransferError, match="unsupported entry"):
        transfer.apply_sync_archive_bytes(root, buf.getvalue(), delete_paths=["stale.txt"])

    assert stale.read_text(encoding="utf-8") == "keep until archive is valid"


# ---------------------------------------------------------------------------
# CLI dispatch (send_files / get_files route to bridge remote when applicable)
# ---------------------------------------------------------------------------

class CapturingFakeBridgeClient(FakeBridgeControlClient):
    """Fake client that also satisfies the minimal interface the CLI uses."""

    pass


def _load_cli():
    pytest.importorskip("typer")
    from typer.testing import CliRunner

    from aimux.cli import app
    from aimux.constants import EXIT_OK, EXIT_USER_ERROR

    return CliRunner, app, EXIT_OK, EXIT_USER_ERROR


def _patch_bridge_remote(monkeypatch, fake):
    def fake_bridge_remote(name):
        return fake, {"name": name, "status": "connected"}
    monkeypatch.setattr("aimux.cli._bridge_remote", fake_bridge_remote)


def test_cli_send_files_dispatches_to_bridge(monkeypatch, tmp_path):
    CliRunner, app, EXIT_OK, _ = _load_cli()
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
    CliRunner, app, EXIT_OK, _ = _load_cli()
    fake = CapturingFakeBridgeClient()
    fake.remote_files["/remote/data.txt"] = b"payload"
    _patch_bridge_remote(monkeypatch, fake)
    monkeypatch.setattr("aimux.cli.ssh_mod.host_exists", lambda name: False)

    dest = tmp_path / "downloads"
    result = CliRunner().invoke(app, ["get_files", "remote-1", str(dest), "/remote/data.txt"])

    assert result.exit_code == EXIT_OK, result.output
    assert "downloaded 1 path(s) from bridge remote remote-1" in result.output
    assert (dest / "data.txt").read_bytes() == b"payload"


def test_cli_sync_dir_dispatches_to_bridge(monkeypatch, tmp_path):
    CliRunner, app, EXIT_OK, _ = _load_cli()
    fake = CapturingFakeBridgeClient()
    _patch_bridge_remote(monkeypatch, fake)
    monkeypatch.setattr("aimux.cli.ssh_mod.host_exists", lambda name: False)

    local = tmp_path / "project"
    local.mkdir()
    (local / "a.txt").write_text("hello", encoding="utf-8")
    (local / "ignored.txt").write_text("ignored", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "sync_dir",
            "--to-remote",
            "--exclude",
            "ignored.txt",
            "remote-1",
            "/remote/project",
            str(local),
        ],
    )

    assert result.exit_code == EXIT_OK, result.output
    assert "sync_dir: scanning local source" in result.output
    assert "sync_dir: plan 1 changed file(s), 0 delete(s), 1 new dir(s)" in result.output
    assert "sync_dir: transferring archive" in result.output
    assert "synced 1 file(s) to bridge remote remote-1:/remote/project" in result.output
    assert fake.remote_files["/remote/project/a.txt"] == b"hello"
    assert "/remote/project/ignored.txt" not in fake.remote_files


def test_cli_send_files_falls_through_to_ssh_error_when_no_bridge(monkeypatch, tmp_path):
    CliRunner, app, _, EXIT_USER_ERROR = _load_cli()
    monkeypatch.setattr("aimux.cli._bridge_remote", lambda name: (None, None))
    monkeypatch.setattr("aimux.cli.ssh_mod.host_exists", lambda name: False)

    result = CliRunner().invoke(app, ["send_files", "unknown-remote", "/out", str(tmp_path / "x")])

    assert result.exit_code == EXIT_USER_ERROR
    assert "not found in ~/.ssh/config or bridge registry" in result.output
