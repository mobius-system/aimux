"""remote_read / remote_edit / remote_write and the patch read gate, at the MCP layer."""
from __future__ import annotations

import base64

import pytest

from aimux import bridge
from aimux import mcp_server


class SpyClient:
    """In-memory remote that records the read policy of every call."""

    def __init__(self, files: dict[str, bytes] | None = None, *, error: Exception | None = None):
        self.files = dict(files or {})
        self.calls: list[tuple[str, str, dict]] = []
        self.error = error

    def _raise(self):
        if self.error is not None:
            raise self.error

    def file_read(self, *, remote, path, timeout=180.0, **kwargs):
        self.calls.append(("read", path, kwargs))
        self._raise()
        if path not in self.files:
            raise bridge.BridgeError(f"not found: {path}", kind="not_found")
        return {"content_b64": base64.b64encode(self.files[path]).decode("ascii")}

    def file_write(self, *, remote, path, content_b64, timeout=180.0, **kwargs):
        self.calls.append(("write", path, kwargs))
        self._raise()
        data = base64.b64decode(content_b64)
        self.files[path] = data
        return {"size": len(data)}

    def file_delete(self, *, remote, path, recursive=False, timeout=60.0, **kwargs):
        self.calls.append(("delete", path, kwargs))
        self._raise()
        self.files.pop(path, None)
        return {"ok": True}


@pytest.fixture
def spy(monkeypatch):
    client = SpyClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: client)
    return client


def test_remote_read_numbers_lines_and_reports_truncation(spy):
    spy.files["/a.txt"] = b"one\ntwo\nthree\n"

    out = mcp_server._remote_read("r", "p", {"file_path": "/a.txt", "offset": 2, "limit": 1})

    assert out["total_lines"] == 3
    assert out["returned_lines"] == 1
    assert out["truncated"] is True
    assert out["content"].endswith("\ttwo")
    assert out["content"].startswith("     2")
    assert spy.calls[0] == ("read", "/a.txt",
                            {"session": mcp_server._ledger_session(), "read_policy": "record"})


def test_remote_read_defaults_to_the_first_2000_lines(spy):
    spy.files["/big.txt"] = ("x\n" * 2500).encode()

    out = mcp_server._remote_read("r", "p", {"file_path": "/big.txt"})

    assert out["returned_lines"] == 2000 and out["truncated"] is True


def test_remote_read_rejects_relative_paths(spy):
    with pytest.raises(ValueError, match="must be absolute"):
        mcp_server._remote_read("r", "p", {"file_path": "a.txt"})


def test_remote_read_empty_file_errors_but_still_records(spy):
    spy.files["/empty.txt"] = b""

    with pytest.raises(ValueError, match="is empty"):
        mcp_server._remote_read("r", "p", {"file_path": "/empty.txt"})

    # The read happened (and was recorded); the caller just has no content.
    assert spy.calls[0][2]["read_policy"] == "record"


def test_remote_edit_replaces_one_occurrence_and_writes_with_the_gate(spy):
    spy.files["/a.py"] = b"print(1)\nprint(9)\n"

    out = mcp_server._remote_edit("r", "p", {
        "file_path": "/a.py", "old_string": "print(1)", "new_string": "print(2)",
    })

    assert out["replacements"] == 1
    assert spy.files["/a.py"] == b"print(2)\nprint(9)\n"
    assert ("read", "/a.py", {"read_policy": "internal"}) in spy.calls
    assert ("write", "/a.py",
            {"session": mcp_server._ledger_session(), "read_policy": "require"}) in spy.calls


def test_remote_edit_replace_all(spy):
    spy.files["/a.py"] = b"x\nx\n"

    out = mcp_server._remote_edit("r", "p", {
        "file_path": "/a.py", "old_string": "x", "new_string": "y", "replace_all": True,
    })

    assert out["replacements"] == 2
    assert spy.files["/a.py"] == b"y\ny\n"


def test_remote_edit_requires_a_unique_match(spy):
    spy.files["/a.py"] = b"x\nx\n"

    with pytest.raises(ValueError, match="matches 2 times"):
        mcp_server._remote_edit("r", "p", {
            "file_path": "/a.py", "old_string": "x", "new_string": "y",
        })
    assert spy.files["/a.py"] == b"x\nx\n"


def test_remote_edit_missing_context_points_at_remote_read(spy):
    spy.files["/a.py"] = b"hello\n"

    with pytest.raises(ValueError) as excinfo:
        mcp_server._remote_edit("r", "p", {
            "file_path": "/a.py", "old_string": "absent", "new_string": "x",
        })

    message = str(excinfo.value)
    assert "edit context not found" in message and "remote_read" in message


def test_remote_edit_rejects_identical_strings(spy):
    with pytest.raises(ValueError, match="must differ"):
        mcp_server._remote_edit("r", "p", {
            "file_path": "/a.py", "old_string": "x", "new_string": "x",
        })


def test_remote_edit_preserves_the_file_encoding(spy):
    spy.files["/gbk.txt"] = "保留文本".encode("cp936")

    mcp_server._remote_edit("r", "p", {
        "file_path": "/gbk.txt", "old_string": "文本", "new_string": "内容",
    })

    assert spy.files["/gbk.txt"] == "保留内容".encode("cp936")


def test_remote_write_sends_the_gate_and_creates_the_file(spy):
    out = mcp_server._remote_write("r", "p", {"file_path": "/new.txt", "content": "hello\n"})

    assert out["bytes"] == 6
    assert spy.files["/new.txt"] == b"hello\n"
    assert ("write", "/new.txt",
            {"session": mcp_server._ledger_session(), "read_policy": "require"}) in spy.calls


def test_remote_write_surfaces_the_client_gate(monkeypatch):
    client = SpyClient(error=bridge.BridgeError(
        "/x.txt has not been read in this session; call remote_read first", kind="read_required"))
    monkeypatch.setattr(mcp_server, "_client", lambda: client)

    with pytest.raises(ValueError, match="has not been read in this session"):
        mcp_server._remote_write("r", "p", {"file_path": "/x.txt", "content": "x"})


def test_apply_patch_reads_internally_and_writes_with_the_gate(spy):
    spy.files["/a.txt"] = b"old\n"

    mcp_server._remote_apply_patch("r", "p", {
        "input": "*** Begin Patch\n*** Update File: /a.txt\n@@\n-old\n+new\n*** End Patch",
    })

    assert spy.files["/a.txt"] == b"new\n"
    assert ("read", "/a.txt", {"read_policy": "internal"}) in spy.calls
    assert ("write", "/a.txt",
            {"session": mcp_server._ledger_session(), "read_policy": "require"}) in spy.calls


def test_apply_patch_add_file_needs_no_prior_read(spy):
    mcp_server._remote_apply_patch("r", "p", {
        "input": "*** Begin Patch\n*** Add File: /new.txt\n+hello\n*** End Patch",
    })

    assert spy.files["/new.txt"] == b"hello"


def test_apply_patch_delete_is_gated(spy):
    spy.files["/a.txt"] = b"old\n"

    mcp_server._remote_apply_patch("r", "p", {
        "input": "*** Begin Patch\n*** Delete File: /a.txt\n*** End Patch",
    })

    assert "/a.txt" not in spy.files
    assert ("delete", "/a.txt",
            {"session": mcp_server._ledger_session(), "read_policy": "require"}) in spy.calls


def test_apply_patch_reports_files_already_changed_when_a_later_gate_fails(monkeypatch):
    class LaterGateClient(SpyClient):
        def file_write(self, *, remote, path, content_b64, timeout=180.0, **kwargs):
            if path == "/b.txt":
                raise bridge.BridgeError(
                    "/b.txt has not been read in this session", kind="read_required")
            return super().file_write(remote=remote, path=path,
                                      content_b64=content_b64, timeout=timeout, **kwargs)

    client = LaterGateClient({"/a.txt": b"old\n", "/b.txt": b"old\n"})
    monkeypatch.setattr(mcp_server, "_client", lambda: client)

    with pytest.raises(ValueError) as excinfo:
        mcp_server._remote_apply_patch("r", "p", {"input": (
            "*** Begin Patch\n"
            "*** Update File: /a.txt\n@@\n-old\n+new\n"
            "*** Update File: /b.txt\n@@\n-old\n+new\n"
            "*** End Patch")})

    message = str(excinfo.value)
    assert "has not been read" in message
    # Never claim the whole patch was atomic.
    assert "already applied: updated: /a.txt" in message
    assert client.files["/a.txt"] == b"new\n"
