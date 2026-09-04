"""remote_download_file — real file transfer from container to local. 6 tests."""
from __future__ import annotations

import hashlib

import pytest


def test_download_to_local_path_size_sha256(mcp, tmp_path):
    remote = tmp_path / "src.bin"
    payload = bytes(range(256)) + b"\xff\x00\xfe"
    remote.write_bytes(payload)
    local = tmp_path / "out" / "copy.bin"
    res = mcp.call("remote_download_file", path=str(remote), local_path=str(local))
    assert res["size"] == len(payload)
    assert res["sha256"] == hashlib.sha256(payload).hexdigest()
    assert local.read_bytes() == payload


def test_download_default_path_is_basename(mcp, mcp_cwd, tmp_path):
    remote = tmp_path / "remote-dir" / "file.txt"
    remote.parent.mkdir()
    remote.write_bytes(b"default-basename")
    # No local_path: defaults to basename inside the MCP server process's
    # cwd, which the harness pins to mcp_cwd.
    res = mcp.call("remote_download_file", path=str(remote))
    expected = (mcp_cwd / "file.txt").resolve()
    assert res["local_path"] == str(expected)
    assert expected.read_bytes() == b"default-basename"


def test_download_overwrite_refused_then_ok(mcp, tmp_path):
    remote = tmp_path / "v.txt"
    remote.write_bytes(b"new")
    local = tmp_path / "local.txt"
    local.write_bytes(b"old")
    with pytest.raises(Exception):
        mcp.call("remote_download_file", path=str(remote), local_path=str(local))
    assert local.read_bytes() == b"old"  # untouched
    res = mcp.call("remote_download_file", path=str(remote), local_path=str(local), overwrite=True)
    assert local.read_bytes() == b"new"
    assert res["size"] == 3


def test_download_creates_parent_dirs(mcp, tmp_path):
    remote = tmp_path / "src.bin"
    remote.write_bytes(b"deep")
    local = tmp_path / "a" / "b" / "c" / "deep.bin"
    mcp.call("remote_download_file", path=str(remote), local_path=str(local))
    assert local.read_bytes() == b"deep"


def test_download_sha256_binary_correctness(mcp, tmp_path):
    remote = tmp_path / "rand.bin"
    payload = bytes((i * 37 + 11) & 0xFF for i in range(1024))
    remote.write_bytes(payload)
    local = tmp_path / "rand-copy.bin"
    res = mcp.call("remote_download_file", path=str(remote), local_path=str(local))
    assert res["sha256"] == hashlib.sha256(payload).hexdigest()
    assert local.read_bytes() == payload


def test_download_local_path_dir_raises(mcp, tmp_path):
    remote = tmp_path / "src.bin"
    remote.write_bytes(b"x")
    (tmp_path / "adir").mkdir()
    with pytest.raises(Exception):
        mcp.call("remote_download_file", path=str(remote), local_path=str(tmp_path / "adir"))
