"""Tests for the top-level `aimux read` and `aimux apply-patch` commands."""
from __future__ import annotations

import base64
import hashlib
import json
import os

import pytest
from typer.testing import CliRunner

from aimux import cli as cli_mod
from aimux.cli import app
from aimux.constants import EXIT_OK, EXIT_USER_ERROR


class FakeClient:
    """In-memory remote FS standing in for the bridge control client."""

    def __init__(self):
        self.files: dict[str, bytes] = {}

    def file_read(self, *, remote, path, timeout=180.0):
        if path not in self.files:
            raise RuntimeError(f"not a regular file: {path}")
        return {"content_b64": base64.b64encode(self.files[path]).decode("ascii")}

    def file_write(self, *, remote, path, content_b64, mode=None, max_size=None, timeout=180.0):
        self.files[path] = base64.b64decode(content_b64)
        return {"ok": True}

    def file_delete(self, *, remote, path, recursive=False, timeout=60.0):
        self.files.pop(path, None)
        return {"ok": True}


@pytest.fixture
def fake_remote(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(cli_mod, "_bridge_remote", lambda name: (c, {"name": name, "status": "connected"}))
    return c


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


def test_read_default_output_header_and_lineno(fake_remote):
    fake_remote.files["/etc/app.conf"] = "alpha\nbeta\n".encode()
    r = CliRunner().invoke(app, ["read", "box", "--path", "/etc/app.conf"])
    assert r.exit_code == EXIT_OK, r.output
    assert "path:     /etc/app.conf" in r.output
    assert "encoding: utf-8" in r.output
    assert "size:     11 bytes (lines 1-2; showing 1-2)" in r.output
    assert f"sha256:   {hashlib.sha256(b'alpha\nbeta\n').hexdigest()}" in r.output
    assert "1→alpha" in r.output and "2→beta" in r.output


def test_read_offset_limit_and_truncated_flag(fake_remote):
    fake_remote.files["/f"] = "\n".join(f"l{i}" for i in range(1, 51)).encode()
    r = CliRunner().invoke(app, ["read", "box", "--path", "/f", "--offset", "10", "--limit", "3", "--json"])
    assert r.exit_code == EXIT_OK, r.output
    obj = json.loads(r.output)
    assert obj["total_lines"] == 50
    assert obj["offset"] == 10
    assert obj["limit"] == 3
    assert obj["truncated"] is True
    assert obj["content"] == "10→l10\n11→l11\n12→l12"


def test_read_no_lineno_and_raw(fake_remote):
    payload = "alpha\nbeta\n".encode()
    fake_remote.files["/f"] = payload
    r = CliRunner().invoke(app, ["read", "box", "--path", "/f", "--no-lineno"])
    assert "alpha\nbeta" in r.output and "→" not in r.output.split("─" * 44)[1]

    r2 = CliRunner().invoke(app, ["read", "box", "--path", "/f", "--raw"])
    assert r2.exit_code == EXIT_OK
    assert r2.stdout_bytes == payload or r2.output.encode() == payload


def test_read_encoding_detection(fake_remote):
    fake_remote.files["/u16"] = "hi".encode("utf-16")
    fake_remote.files["/bom"] = b"\xef\xbb\xbfhi"
    fake_remote.files["/latin"] = "café".encode("latin-1")
    for path, enc in [("/u16", "utf-16"), ("/bom", "utf-8-sig"), ("/latin", "latin-1")]:
        r = CliRunner().invoke(app, ["read", "box", "--path", path])
        assert f"encoding: {enc}" in r.output, (path, r.output)


def test_read_binary_summary(fake_remote):
    blob = b"\x89PNG\r\n\x1a\n" + bytes(200)
    fake_remote.files["/img.png"] = blob
    r = CliRunner().invoke(app, ["read", "box", "--path", "/img.png", "--json"])
    obj = json.loads(r.output)
    assert obj["encoding"] == "binary"
    assert obj["total_lines"] == 0
    assert base64.b64decode(obj["content_b64"]) == blob


def test_read_rejects_relative_path(fake_remote):
    r = CliRunner().invoke(app, ["read", "box", "--path", "src/x.py"])
    assert r.exit_code == EXIT_USER_ERROR
    assert "absolute" in r.output


def test_read_offset_beyond_eof(fake_remote):
    fake_remote.files["/f"] = b"a\nb\n"
    r = CliRunner().invoke(app, ["read", "box", "--path", "/f", "--offset", "99", "--json"])
    obj = json.loads(r.output)
    assert obj["content"] == "" and obj["offset"] == 3


# ---------------------------------------------------------------------------
# apply-patch
# ---------------------------------------------------------------------------


def test_apply_patch_local_update_add_delete(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("alpha\nmiddle\nomega\n")
    new = tmp_path / "new.txt"
    patch = (
        f"*** Begin Patch\n"
        f"*** Update File: {f}\n@@\n-alpha\n+ALPHA\n"
        f"*** Add File: {new}\n+created\n"
        f"*** Delete File: {tmp_path / 'old.txt'}\n"
        f"*** End Patch\n"
    )
    (tmp_path / "old.txt").write_text("stale")
    r = CliRunner().invoke(app, ["apply-patch"], input=patch)
    assert r.exit_code == EXIT_OK, r.output
    assert f.read_text() == "ALPHA\nmiddle\nomega\n"
    assert new.read_text() == "created"
    assert not (tmp_path / "old.txt").exists()


def test_apply_patch_local_dry_run_leaves_files(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("alpha\n")
    patch = f"*** Begin Patch\n*** Update File: {f}\n@@\n-alpha\n+ALPHA\n*** End Patch\n"
    r = CliRunner().invoke(app, ["apply-patch", "--dry-run"], input=patch)
    assert r.exit_code == EXIT_OK, r.output
    assert "would update" in r.output
    assert f.read_text() == "alpha\n"  # untouched


def test_apply_patch_local_context_mismatch_fails(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("real\n")
    patch = f"*** Begin Patch\n*** Update File: {f}\n@@\n-not-in-file\n+x\n*** End Patch\n"
    r = CliRunner().invoke(app, ["apply-patch"], input=patch)
    assert r.exit_code != EXIT_OK
    assert isinstance(r.exception, ValueError)
    assert "patch context not found" in str(r.exception)
    assert f.read_text() == "real\n"  # untouched


def test_apply_patch_rejects_relative_paths(tmp_path):
    patch = "*** Begin Patch\n*** Update File: rel/x.py\n@@\n-a\n+b\n*** End Patch\n"
    r = CliRunner().invoke(app, ["apply-patch"], input=patch)
    assert r.exit_code == EXIT_USER_ERROR
    assert "must be absolute" in r.output and "rel/x.py" in r.output


def test_apply_patch_add_refuses_existing_without_force(tmp_path):
    f = tmp_path / "exists.txt"
    f.write_text("keep")
    patch = f"*** Begin Patch\n*** Add File: {f}\n+overwrite\n*** End Patch\n"
    r = CliRunner().invoke(app, ["apply-patch"], input=patch)
    assert r.exit_code == EXIT_USER_ERROR
    assert f.read_text() == "keep"
    r2 = CliRunner().invoke(app, ["apply-patch", "--force"], input=patch)
    assert r2.exit_code == EXIT_OK
    assert f.read_text() == "overwrite"


def test_apply_patch_bridge_channel(fake_remote, monkeypatch):
    """--remote hitting a bridge goes through mcp_server._remote_apply_patch."""
    from aimux import mcp_server

    fake_remote.files["/f"] = b"foo\nold\nbar\n"
    monkeypatch.setattr(mcp_server, "_client", lambda: fake_remote)
    patch = "*** Begin Patch\n*** Update File: /f\n@@\n-old\n+new\n*** End Patch\n"
    r = CliRunner().invoke(app, ["apply-patch", "--remote", "box"], input=patch)
    assert r.exit_code == EXIT_OK, r.output
    assert "updated: /f" in r.output
    assert fake_remote.files["/f"] == b"foo\nnew\nbar\n"


def test_apply_patch_bridge_dry_run_no_write(fake_remote, monkeypatch):
    from aimux import mcp_server

    fake_remote.files["/f"] = b"foo\nold\nbar\n"
    monkeypatch.setattr(mcp_server, "_client", lambda: fake_remote)
    patch = "*** Begin Patch\n*** Update File: /f\n@@\n-old\n+new\n*** End Patch\n"
    r = CliRunner().invoke(app, ["apply-patch", "--remote", "box", "--dry-run"], input=patch)
    assert r.exit_code == EXIT_OK, r.output
    assert "would update: /f" in r.output
    assert fake_remote.files["/f"] == b"foo\nold\nbar\n"  # untouched


def test_apply_patch_from_file(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x\n")
    pfile = tmp_path / "p.txt"
    pfile.write_text(f"*** Begin Patch\n*** Update File: {f}\n@@\n-x\n+y\n*** End Patch\n")
    r = CliRunner().invoke(app, ["apply-patch", "--file", str(pfile)])
    assert r.exit_code == EXIT_OK, r.output
    assert f.read_text() == "y\n"
