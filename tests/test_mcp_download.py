"""Tests for the remote_download_file MCP tool."""
from __future__ import annotations

import base64
import hashlib

import pytest

from aimux import mcp_server

from test_mcp_server import FakeClient


@pytest.fixture
def fake(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    return c


def _call(tmp_path, monkeypatch, **tool_args):
    monkeypatch.chdir(tmp_path)
    return mcp_server._TOOL_FUNCS["remote_download_file"]("remote_1", "posix-pty", tool_args)


def test_download_writes_content_size_and_sha256(fake, tmp_path, monkeypatch):
    payload = b"hello download\x00\xff"
    fake.files["/remote/data.bin"] = payload

    out = _call(tmp_path, monkeypatch, path="/remote/data.bin")

    local = tmp_path / "data.bin"
    assert local.read_bytes() == payload
    assert out["local_path"] == str(local)
    assert out["size"] == len(payload)
    assert out["sha256"] == hashlib.sha256(payload).hexdigest()


def test_download_default_path_is_basename_not_remote_dirs(fake, tmp_path, monkeypatch):
    fake.files["/deep/nested/file.txt"] = b"x"

    out = _call(tmp_path, monkeypatch, path="/deep/nested/file.txt")

    # basename only — remote directory structure must not leak into cwd
    assert (tmp_path / "file.txt").read_bytes() == b"x"
    assert out["local_path"] == str(tmp_path / "file.txt")
    assert not (tmp_path / "deep").exists()


def test_download_explicit_local_path_creates_parent_dirs(fake, tmp_path, monkeypatch):
    fake.files["/remote/a.log"] = b"log-line"
    target = tmp_path / "out" / "sub" / "a.log"

    out = _call(tmp_path, monkeypatch, path="/remote/a.log", local_path=str(target))

    assert target.read_bytes() == b"log-line"
    assert out["local_path"] == str(target)


def test_download_refuses_overwrite_without_flag(fake, tmp_path, monkeypatch):
    fake.files["/remote/f.txt"] = b"new"
    (tmp_path / "f.txt").write_bytes(b"old")

    with pytest.raises(FileExistsError):
        _call(tmp_path, monkeypatch, path="/remote/f.txt")

    assert (tmp_path / "f.txt").read_bytes() == b"old"  # untouched

    out = _call(tmp_path, monkeypatch, path="/remote/f.txt", overwrite=True)
    assert (tmp_path / "f.txt").read_bytes() == b"new"
    assert out["size"] == 3


def test_download_rejects_directory_local_path(fake, tmp_path, monkeypatch):
    (tmp_path / "adir").mkdir()
    fake.files["/remote/f.txt"] = b"x"

    with pytest.raises(ValueError):
        _call(tmp_path, monkeypatch, path="/remote/f.txt", local_path=str(tmp_path / "adir"))

    with pytest.raises(ValueError):
        _call(tmp_path, monkeypatch, path="/remote/f.txt", local_path="  ")

    with pytest.raises(ValueError):
        _call(tmp_path, monkeypatch, path="")


def test_download_propagates_remote_errors(fake, tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="not found"):
        _call(tmp_path, monkeypatch, path="/remote/missing.txt")


def test_download_registered_in_tools_and_handlers():
    names = [t["name"] for t in mcp_server.TOOLS]
    assert "remote_download_file" in names
    assert "remote_download_file" in mcp_server._TOOL_FUNCS

    schema = next(t for t in mcp_server.TOOLS if t["name"] == "remote_download_file")["inputSchema"]
    assert schema["required"] == ["path"]
    assert set(schema["properties"]) == {"path", "local_path", "overwrite"}
    assert schema["properties"]["overwrite"]["default"] is False
