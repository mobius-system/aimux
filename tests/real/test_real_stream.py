"""Streaming file read — raw 1 MiB chunked transfer over the independent data
connection (files >= STREAM_THRESHOLD). Exercises the large-file path end to
end: CLI -> broker -> reverse client -> container FS, and back as raw bytes."""
from __future__ import annotations

import hashlib

import pytest

from aimux import transfer
from aimux.bridge import BridgeError
from aimux.transfer import STREAM_THRESHOLD

REMOTE = "localbox"


def _big_bytes(n: int) -> bytes:
    """Deterministic, incompressible-ish payload generated in a 64 KiB block."""
    block = bytes((i * 131 + 17) & 0xFF for i in range(65536))
    return (block * ((n // len(block)) + 1))[:n]


def test_stream_read_large_file_matches(broker, tmp_path):
    remote = tmp_path / "big.bin"
    size = STREAM_THRESHOLD + (2 * 1024 * 1024)
    payload = _big_bytes(size)
    remote.write_bytes(payload)

    data = broker.file_read_stream(remote=REMOTE, path=str(remote))

    assert data == payload
    assert hashlib.sha256(data).hexdigest() == hashlib.sha256(payload).hexdigest()


def test_get_files_routes_large_file_through_stream(broker, tmp_path):
    remote = tmp_path / "getme.bin"
    size = STREAM_THRESHOLD + 1024
    payload = _big_bytes(size)
    remote.write_bytes(payload)

    local_dir = tmp_path / "out"
    transfer.bridge_get_files(broker, REMOTE, [str(remote)], local_dir)

    out_file = local_dir / "getme.bin"
    assert out_file.read_bytes() == payload


def test_stream_read_small_file_still_works(broker, tmp_path):
    remote = tmp_path / "small.txt"
    payload = b"small file, below the stream threshold"
    remote.write_bytes(payload)

    data = broker.file_read_stream(remote=REMOTE, path=str(remote))
    assert data == payload


def test_stream_read_missing_file_raises(broker, tmp_path):
    with pytest.raises(BridgeError):
        broker.file_read_stream(remote=REMOTE, path=str(tmp_path / "does-not-exist.bin"))


def test_stream_write_large_file_matches(broker, tmp_path):
    size = STREAM_THRESHOLD + (2 * 1024 * 1024)
    payload = _big_bytes(size)
    remote = tmp_path / "uploaded.bin"

    res = broker.file_write_stream(remote=REMOTE, path=str(remote), data=payload)

    assert res.get("ok") is True
    assert remote.read_bytes() == payload


def test_send_files_routes_large_file_through_stream(broker, tmp_path):
    size = STREAM_THRESHOLD + 1024
    payload = _big_bytes(size)
    local = tmp_path / "local_src.bin"
    local.write_bytes(payload)
    remote_dir = tmp_path / "destdir"

    transfer.bridge_send_files(broker, REMOTE, [str(local)], str(remote_dir))

    out_file = remote_dir / "local_src.bin"
    assert out_file.read_bytes() == payload
