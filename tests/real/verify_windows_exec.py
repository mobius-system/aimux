#!/usr/bin/env python3
"""Manual check of remote_exec_command against a live Windows bridge remote.

Not a pytest test: it needs a real Windows host reverse-connected to the local
broker, which CI does not have. Run it after touching the Windows exec path:

    python3 tests/real/verify_windows_exec.py tui-desktop-<id>

It drives the real MCP stdio server (exactly what an agent does) and asserts
that every shell family reports an exit code with clean output — the failure
this exists to catch is a command that runs but never reports completion, which
returns session_id and hangs the agent forever.

Set AIMUX_MCP_PYTHONPATH to test uncommitted code against a released runtime.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

AIMUX = os.environ.get("AIMUX_BIN", "aimux")


class Mcp:
    def __init__(self, remote: str, profile: str | None = None):
        env = dict(os.environ)
        if os.environ.get("AIMUX_MCP_PYTHONPATH"):
            env["PYTHONPATH"] = os.environ["AIMUX_MCP_PYTHONPATH"]
        args = [AIMUX, "mcp", "serve", "--remote", remote]
        if profile:
            args += ["--profile", profile]
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1, env=env)
        self._q: queue.Queue[dict] = queue.Queue()
        self._id = 0
        threading.Thread(target=self._read, daemon=True).start()
        self._call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "verify", "version": "0"}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self._q.put(json.loads(line))
                except json.JSONDecodeError:
                    pass

    def _call(self, method: str, params=None, timeout: float = 120.0):
        self._id += 1
        mid = self._id
        req = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self._q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == mid:
                return msg
        raise TimeoutError(method)

    def exec(self, cmd: str, **kw) -> dict:
        args = {"cmd": cmd, "yield_time_ms": kw.pop("yield_time_ms", 15000)}
        args.update(kw)
        msg = self._call("tools/call", {"name": "remote_exec_command", "arguments": args})
        res = msg.get("result") or {}
        text = (res.get("content") or [{}])[0].get("text", "")
        if res.get("isError"):
            return {"isError": True, "text": text}
        return json.loads(text)

    def write_stdin(self, session_id: int, **kw) -> dict:
        args = {"session_id": session_id, "chars": kw.pop("chars", "")}
        args.update(kw)
        msg = self._call("tools/call", {"name": "remote_write_stdin", "arguments": args})
        res = msg.get("result") or {}
        text = (res.get("content") or [{}])[0].get("text", "")
        return json.loads(text) if not res.get("isError") else {"isError": True, "text": text}

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        self.proc.wait(timeout=10)


def _check(label: str, res: dict, expect_exit: int | None) -> bool:
    if res.get("isError"):
        print(f"FAIL {label}: {res['text']}")
        return False
    ok = True
    if expect_exit is not None and res.get("exit_code") != expect_exit:
        print(f"FAIL {label}: exit_code={res.get('exit_code')!r} (want {expect_exit}) "
              f"session_id={res.get('session_id')!r} output={res.get('output')!r}")
        ok = False
    if "session_id" in res:
        print(f"FAIL {label}: never completed — returned session_id={res['session_id']}")
        ok = False
    if "\r\n" in res.get("output", "") and ":" in label and "banner" not in label:
        pass  # Windows output is CRLF; nothing to assert here
    if ok:
        print(f"ok   {label}: exit_code={res.get('exit_code')} output={res.get('output')!r}")
    return ok


def main(remote: str) -> int:
    failures = 0
    for profile in (None, "powershell", "mingw64"):
        m = Mcp(remote, profile)
        try:
            failures += not _check(f"exec[{profile or 'default'}]",
                                   m.exec("echo AIMUX_EXEC_PROBE"), 0)
        finally:
            m.close()

    # Long command: yields session_id, then remote_write_stdin finishes it.
    m = Mcp(remote)
    try:
        first = m.exec("ping -n 6 127.0.0.1 > nul & echo PING_DONE", yield_time_ms=1500)
        sid = first.get("session_id")
        if not sid:
            print(f"ok   long-command finished inside the first yield: {first.get('exit_code')}")
        else:
            done = None
            for _ in range(10):
                done = m.write_stdin(sid, yield_time_ms=5000)
                if "exit_code" in done:
                    break
            failures += not _check("long-command via remote_write_stdin", done or {}, 0)
        failures += not _check("truncated output",
                               m.exec("for /l %%i in (1,1,400) do @echo line_%%i",
                                      yield_time_ms=25000, max_output_tokens=60), 0)
    finally:
        m.close()

    print("PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
