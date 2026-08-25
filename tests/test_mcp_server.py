"""Unit tests for aimux.mcp_server (stdio MCP server, codex-facing tools)."""
import base64

import pytest

from aimux import mcp_server


class ExecClient:
    def __init__(self):
        self.sent = []
        self.captures = []
        self.killed = []

    def create_session(self, **kwargs):
        return {"ok": True}

    def send_keys(self, **kwargs):
        self.sent.append(kwargs)
        return {"ok": True}

    def capture(self, **kwargs):
        return self.captures.pop(0) if self.captures else {"content": ""}

    def kill_session(self, **kwargs):
        self.killed.append(kwargs)
        return {"ok": True}


class FakeClient:
    """Stand-in for BridgeControlClient; keeps an in-memory remote filesystem."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.health_called = False

    def health(self, *, timeout=2.0):
        self.health_called = True
        return {"ok": True}

    def get_remote(self, name, *, timeout=2.0):
        return {"name": name, "default_profile": "posix-pty", "status": "connected"}

    def file_read(self, *, remote, path, timeout=180.0):
        if path not in self.files:
            raise RuntimeError(f"not found: {path}")
        return {"content_b64": base64.b64encode(self.files[path]).decode()}

    def file_write(self, *, remote, path, content_b64, mode=None, max_size=None, timeout=180.0):
        self.files[path] = base64.b64decode(content_b64)
        return {"ok": True}

    def file_delete(self, *, remote, path, recursive=False, timeout=60.0):
        self.files.pop(path, None)
        return {"ok": True}


@pytest.fixture
def fake(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    return c


# ---- patch parser & applier ----

def test_parse_add_update_delete():
    ops = mcp_server._parse_codex_patch(
        "*** Begin Patch\n"
        "*** Add File: a.txt\n+hello\n"
        "*** Update File: b.txt\n@@\n-old\n+new\n"
        "*** Delete File: c.txt\n*** End Patch\n"
    )
    assert len(ops) == 3
    assert ops[0] == {"kind": "add", "path": "a.txt", "content": "hello"}
    assert ops[1]["kind"] == "update" and ops[1]["path"] == "b.txt"
    assert ops[2] == {"kind": "delete", "path": "c.txt"}


def test_apply_hunks_simple():
    new = mcp_server._apply_hunks("foo\nold\nbar\n", [{"lines": ["@@", "-old", "+new"]}])
    assert new == "foo\nnew\nbar\n"


def test_apply_hunks_with_context():
    new = mcp_server._apply_hunks("a\nb\nc\nd\n", [{"lines": ["@@", " b", "-c", "+C", " d"]}])
    assert new == "a\nb\nC\nd\n"


def test_apply_hunks_rejects_missing_context():
    with pytest.raises(ValueError, match="patch context not found"):
        mcp_server._apply_hunks("a\nb\n", [{"lines": ["@@", "-missing", "+new"]}])


# ---- tools ----

def test_view_image(fake):
    fake.files["/x.png"] = b"\x89PNG\r\n fake image bytes"
    r = mcp_server._remote_view_image("r", "p", {"path": "/x.png"})
    assert r["detail"] == "high"
    assert r["image_url"].startswith("data:image/png;base64,")
    assert base64.b64decode(r["image_url"].split(",", 1)[1]) == fake.files["/x.png"]


def test_view_image_jpeg_and_detail(fake):
    fake.files["/y.JPG"] = b"jpeg bytes"
    r = mcp_server._remote_view_image("r", "p", {"path": "/y.JPG", "detail": "original"})
    assert r["detail"] == "original"
    assert r["image_url"].startswith("data:image/jpeg;base64,")


def test_ping(fake):
    assert mcp_server._remote_ping("r", "p", {}) == {"ok": True}
    assert fake.health_called


def test_write_stdin_control_key_is_raw(monkeypatch):
    c = ExecClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()
    sid = mcp_server._create_exec_session("r", "posix-pty", "less file", None)
    c.captures = [{"content": ""}]

    result = mcp_server._remote_write_stdin("r", "posix-pty", {"session_id": sid, "chars": "q", "yield_time_ms": 250})

    assert c.sent[0]["keys"] == ["q"]
    assert c.sent[1]["keys"][0].startswith("echo __AIMUX_EXIT_")
    assert result["session_id"] == sid


def test_write_stdin_command_keeps_implicit_enter_and_supports_override(monkeypatch):
    c = ExecClient()
    monkeypatch.setattr(mcp_server, "_client", lambda: c)
    mcp_server._sessions.clear()
    sid = mcp_server._create_exec_session("r", "posix-pty", "bash", None)
    c.captures = [{"content": ""}, {"content": ""}]

    mcp_server._remote_write_stdin("r", "posix-pty", {"session_id": sid, "chars": "echo ready", "yield_time_ms": 250})
    assert c.sent[0]["keys"] == ["echo ready", "Enter"]

    c.sent.clear()
    c.captures = [{"content": ""}]
    mcp_server._remote_write_stdin("r", "posix-pty", {"session_id": sid, "chars": "raw", "append_enter": False, "yield_time_ms": 250})
    assert c.sent[0]["keys"] == ["raw"]


def test_apply_patch_end_to_end(fake):
    fake.files["/f"] = b"foo\nold\nbar\n"
    r = mcp_server._remote_apply_patch(
        "r", "p",
        {"input": "*** Begin Patch\n*** Update File: /f\n@@\n-old\n+new\n*** End Patch\n"},
    )
    assert r["applied"] == ["updated: /f"]
    assert fake.files["/f"] == b"foo\nnew\nbar\n"


def test_apply_patch_add_and_delete(fake):
    r = mcp_server._remote_apply_patch(
        "r", "p",
        {"input": "*** Begin Patch\n*** Add File: /n\n+hello world\n*** Delete File: /g\n*** End Patch\n"},
    )
    assert r["applied"] == ["added: /n", "deleted: /g"]
    assert fake.files["/n"] == b"hello world"
    assert "/g" not in fake.files


def test_apply_patch_relative_path_requires_workdir(fake):
    with pytest.raises(ValueError, match="requires workdir"):
        mcp_server._remote_apply_patch(
            "r", "p",
            {"input": "*** Begin Patch\n*** Add File: src/conv.py\n+hello\n*** End Patch\n"},
        )


def test_apply_patch_relative_path_uses_workdir(fake):
    result = mcp_server._remote_apply_patch(
        "r", "p",
        {
            "workdir": "/workspace/project",
            "input": "*** Begin Patch\n*** Add File: src/conv.py\n+hello\n*** End Patch\n",
        },
    )
    assert result["applied"] == ["added: /workspace/project/src/conv.py"]
    assert fake.files["/workspace/project/src/conv.py"] == b"hello"


# ---- JSON-RPC dispatch ----

def test_jsonrpc_initialize(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert sent and sent[0]["result"]["protocolVersion"]
    assert "tools" in sent[0]["result"]["capabilities"]


def test_jsonrpc_tools_list(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in sent[0]["result"]["tools"]]
    assert names == ["remote_exec_command", "remote_write_stdin", "remote_apply_patch", "remote_view_image", "remote_ping"]


def test_jsonrpc_tools_call_ping(monkeypatch, fake):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "remote_ping", "arguments": {}},
    })
    assert sent[0]["result"]["content"][0]["text"] == '{"ok": true}'


def test_jsonrpc_unknown_tool(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    })
    assert sent[0]["error"]["code"] == -32601


def test_jsonrpc_initialized_notification_no_response(monkeypatch):
    sent = []
    monkeypatch.setattr(mcp_server, "_send", lambda obj: sent.append(obj))
    mcp_server._dispatch("rem", "prof", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert sent == []  # notifications get no response
