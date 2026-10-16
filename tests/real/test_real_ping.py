"""remote_ping + JSON-RPC dispatch — real broker round-trips. 6 tests."""
from __future__ import annotations

import json
import time

import pytest

from aimux import __version__


def test_ping_returns_ok(mcp):
    res = mcp.call("remote_ping")
    assert res.get("ok") is True, res


def test_ping_describes_the_remote_machine(mcp):
    """The whole point: the agent learns where its commands land."""
    res = mcp.call("remote_ping")
    assert res["live"] is True, res
    system = res["system"]
    # This container is Linux; the reverse client reports its own kernel.
    assert system["os"] == "linux", system
    assert system["path_style"] == "posix"
    assert system["home"] and system["cwd"] and system["user"]
    assert system["python"] and system["aimux_version"]
    # Source-tree version: the container installs this checkout editable.
    assert system["aimux_version"] == __version__, system
    # The bound profile is the one the registration advertised.
    assert res["session"]["profile"] == "posix-pty"
    assert res["session"]["shell_family"] == "posix"
    assert [s["name"] for s in res["shells"]] == ["posix-pty"]


def test_ping_summary_is_a_sentence_about_the_remote(mcp):
    res = mcp.call("remote_ping")
    summary = res["summary"]
    assert summary.startswith(f"remote {res['remote']}: Linux")
    assert "posix-pty" in summary
    assert "POSIX syntax" in summary


def test_ping_via_jsonrpc_content_is_json(mcp):
    # call() already json.loads the content text; assert the raw shape too.
    rid = mcp._next()
    mcp._send({
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": "remote_ping", "arguments": {}},
    })
    r = mcp._recv()
    text = r["result"]["content"][0]["text"]
    assert json.loads(text)["ok"] is True


def test_tools_list_classic_tools_in_order(mcp):
    tools = mcp.tools_list()
    names = [t["name"] for t in tools]
    assert names == [
        "remote_exec_command", "remote_set_remote_workdir", "remote_write_stdin",
        "remote_apply_patch", "remote_view_image", "remote_download_file", "remote_ping",
    ]


def test_ping_is_fast(mcp):
    t0 = time.time()
    mcp.call("remote_ping")
    assert (time.time() - t0) < 2.0


def test_ping_twice_both_ok(mcp):
    a = mcp.call("remote_ping")
    b = mcp.call("remote_ping")
    assert a.get("ok") and b.get("ok")


def test_unknown_tool_returns_dispatch_error(mcp):
    rid = mcp._next()
    mcp._send({
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    })
    r = mcp._recv()
    # Unknown tool → JSON-RPC error -32601 (method not found, per _dispatch).
    assert r["error"]["code"] == -32601, r
