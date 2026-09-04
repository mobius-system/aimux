"""Real-environment pytest harness for the 6 aimux MCP tools.

No mocks, no fakes. This starts, inside the container:

  * a real aimux bridge broker  (``aimux bridge deploy``) — writes
    ~/.aimux/bridge/runtime.json and serves the JSON HTTP control API;
  * a real reverse-connect client (``aimux reverse connect``) that registers
    *this* container as remote ``localbox`` and runs the real tmux PTYs
    on this kernel via ``PosixPtyAdapter``;
  * a real MCP stdio server       (``aimux mcp serve --remote localbox``)
    spoken to via newline-delimited JSON-RPC 2.0 over its stdin/stdout.

Every tool call in the tests exercises the full path:

    test  ->  JSON-RPC stdin  ->  MCP server  ->  BridgeControlClient
           ->  broker HTTP    ->  reverse client  ->  real tmux PTY / FS

So an assertion like ``"hello" in output`` proves the command actually ran
in a real shell on a real kernel, was captured from a real PTY, and round-
tripped back through the broker to the test. The interactive tests
(remote_write_stdin) drive real vim, less, tmux, and a python REPL.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from aimux import bridge
from aimux.bridge import BridgeControlClient

PORT = int(os.environ.get("AIMUX_REAL_PORT", "8865"))
TOKEN = os.environ.get("AIMUX_REAL_TOKEN", "realtok")
REMOTE = os.environ.get("AIMUX_REAL_REMOTE", "localbox")
BASE_URL = f"http://127.0.0.1:{PORT}"

# Per-test timeout for an MCP tool call: exec can block up to yield_time_ms,
# so allow generous headroom.
MCP_CALL_TIMEOUT = 60.0


# ---------------------------------------------------------------------------
# session-scoped: bridge broker + reverse-connect remote
# ---------------------------------------------------------------------------


def _wait_for(predicate, *, timeout: float = 20.0, interval: float = 0.25):
    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception as exc:  # broker not up yet, etc.
            last_exc = exc
        time.sleep(interval)
    if last_exc:
        print(f"[conftest] wait exhausted; last error: {last_exc}", flush=True)
    return False


@pytest.fixture(scope="session")
def broker(tmp_path_factory):
    """Start a real bridge broker on 127.0.0.1:PORT. Returns the control client."""
    # Stale runtime.json from a previous run would point at a dead broker; clear.
    try:
        bridge.clear_runtime()
    except Exception:
        pass

    log_path = tmp_path_factory.mktemp("broker") / "broker.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        ["aimux", "bridge", "deploy", "--port", str(PORT), "--host", "127.0.0.1", "--token", TOKEN],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        up = _wait_for(_broker_up, timeout=20.0)
        if not up:
            raise RuntimeError(
                f"bridge broker did not come up; log:\n{log_path.read_text()}"
            )
        client = BridgeControlClient.from_runtime()
        assert client.health(timeout=5.0).get("ok") is True
        yield client
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        log.close()
        try:
            bridge.clear_runtime()
        except Exception:
            pass


def _broker_up() -> bool:
    try:
        return BridgeControlClient.from_runtime().health(timeout=2.0).get("ok") is True
    except Exception:
        return False


@pytest.fixture(scope="session")
def remote_daemon(broker, tmp_path_factory):
    """Start a real reverse-connect client registering `localbox`."""
    log_path = tmp_path_factory.mktemp("reverse") / "reverse.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [
            "aimux", "reverse", "connect", BASE_URL,
            "--identifier", REMOTE, "--token", TOKEN, "--replace",
        ],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        up = _wait_for(lambda: broker.get_remote(REMOTE, timeout=3.0) is not None, timeout=20.0)
        if not up:
            raise RuntimeError(
                f"reverse-connect did not register {REMOTE!r}; log:\n{log_path.read_text()}"
            )
        yield proc
    finally:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        log.close()


def _kill_all_remote_sessions(broker: BridgeControlClient) -> None:
    """Best-effort: reap every tmux PTY session on the remote so tests don't
    bleed into each other. Missing/gone sessions are treated as success by
    the bridge, so this is idempotent."""
    try:
        for sess in broker.list_sessions(remote=REMOTE, timeout=5.0):
            name = sess.get("name")
            if name:
                try:
                    broker.kill_session(remote=REMOTE, name=name, timeout=5.0)
                except Exception:
                    pass
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clean_remote_sessions(broker, remote_daemon):
    """Before AND after each test: kill any leftover PTY sessions on localbox."""
    _kill_all_remote_sessions(broker)
    yield
    _kill_all_remote_sessions(broker)


# ---------------------------------------------------------------------------
# MCP stdio client
# ---------------------------------------------------------------------------


class McpClient:
    """Drive a real `aimux mcp serve --remote localbox` subprocess over stdio."""

    def __init__(self, cwd: str | None = None):
        self.proc = subprocess.Popen(
            ["aimux", "mcp", "serve", "--remote", REMOTE],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            # cwd pins where default local_path resolution (basename in the
            # server's cwd) lands — needed by remote_download_file tests.
            cwd=cwd,
            # New session so a SIGTERM to the broker/remote above does not take
            # the MCP server with it mid-handshake during teardown.
            start_new_session=True,
            text=True, bufsize=1,
        )
        self._id = 0
        # Initialize handshake (required before tools/call by MCP servers).
        init = self._request({"jsonrpc": "2.0", "id": self._next(), "method": "initialize"})
        assert "result" in init and "protocolVersion" in init["result"], init
        # initialized notification — no response expected.
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    # -- low-level stdio ----------------------------------------------------

    def _send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False) + "\n"
        assert self.proc.stdin is not None
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            err = ""
            if self.proc.poll() is not None and self.proc.stderr is not None:
                err = self.proc.stderr.read()
            raise RuntimeError(f"MCP server closed stdout; rc={self.proc.returncode}; stderr={err}")
        return json.loads(line)

    def _request(self, obj: dict) -> dict:
        self._send(obj)
        return self._recv()

    def _next(self) -> int:
        self._id += 1
        return self._id

    # -- public API ---------------------------------------------------------

    def tools_list(self) -> list[dict]:
        r = self._request({"jsonrpc": "2.0", "id": self._next(), "method": "tools/list"})
        return r["result"]["tools"]

    def call(self, tool: str, **args) -> dict:
        """Call a tool; return the parsed result dict. Raises on MCP isError."""
        rid = self._next()
        r = self._request({
            "jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        })
        if "error" in r:
            raise RuntimeError(f"MCP error calling {tool}: {r['error']}")
        result = r["result"]
        content = result.get("content") or []
        text = content[0]["text"] if content else ""
        if result.get("isError"):
            raise RuntimeError(f"{tool} returned isError: {text}")
        # Tool handlers return json.dumps(res) as the content text.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw": text}

    def close(self) -> None:
        try:
            if self.proc.stdin is not None:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass


@pytest.fixture(scope="session")
def mcp_cwd(tmp_path_factory):
    """Fixed cwd for the MCP server subprocess: default local_path resolution
    in remote_download_file (basename in the server process's cwd) lands here."""
    return tmp_path_factory.mktemp("mcp_cwd")


@pytest.fixture(scope="session")
def mcp(remote_daemon, mcp_cwd):
    """A session-scoped real MCP server subprocess spoken to over stdio.

    Spawn is retried: on a cold container the reverse-connect registration
    can beat the broker's remote listing by a moment, and `mcp serve` exits
    rc=2 ("remote not found") if it spawns into that gap.
    """
    last_exc = None
    for _ in range(5):
        try:
            client = McpClient(cwd=str(mcp_cwd))
            break
        except RuntimeError as exc:
            last_exc = exc
            time.sleep(1.0)
    else:
        raise RuntimeError(f"MCP server failed to start after retries: {last_exc}")
    try:
        yield client
    finally:
        client.close()


# ---------------------------------------------------------------------------
# helpers for the interactive tests
# ---------------------------------------------------------------------------


def wait_for_output(mcp: McpClient, sid: int, needle: str, *, timeout: float = 15.0,
                    interval: float = 0.5) -> str:
    """Poll an exec session via empty remote_write_stdin until `needle` shows up
    in the captured output (or timeout). Returns the final captured output."""
    deadline = time.time() + timeout
    out = ""
    while time.time() < deadline:
        res = mcp.call("remote_write_stdin", session_id=sid, chars="", yield_time_ms=int(interval * 1000))
        out = res.get("output", "")
        if needle in out:
            return out
        if res.get("finished"):
            return out
    return out
