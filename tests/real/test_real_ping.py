"""remote_ping + JSON-RPC dispatch — real broker round-trips. 6 tests."""
from __future__ import annotations

import json
import time

import pytest


def test_ping_returns_ok(mcp):
    res = mcp.call("remote_ping")
    assert res.get("ok") is True, res


def test_ping_via_jsonrpc_content_is_json(mcp):
    # call() already json.loads the content text; assert the raw shape too.
    rid = mcp._next()
    mcp._send({
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": "remote_ping", "arguments": {}},
    })
    r = mcp._recv()
    text = r["result"]["content"][0]["text"]
    assert json.loads(text) == {"ok": True}


def test_tools_list_six_tools_in_order(mcp):
    tools = mcp.tools_list()
    names = [t["name"] for t in tools]
    assert names == [
        "remote_exec_command", "remote_write_stdin", "remote_apply_patch",
        "remote_view_image", "remote_download_file", "remote_ping",
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
