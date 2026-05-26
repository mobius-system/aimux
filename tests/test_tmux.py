from __future__ import annotations

import subprocess

from aimux import tmux
from aimux.constants import SOCKET_NAME


def test_has_session_returns_false_when_server_is_not_running(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, *, capture_output, text, **kwargs):
        calls.append(args)
        assert capture_output is True
        assert text is True
        if args == ["tmux", "-L", SOCKET_NAME, "ls"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout="",
                stderr="no server running on /tmp/tmux-test/aimux",
            )
        raise AssertionError(f"unexpected tmux command: {args}")

    monkeypatch.setattr(tmux.subprocess, "run", fake_run)

    assert tmux.has_session("task") is False
    assert calls == [["tmux", "-L", SOCKET_NAME, "ls"]]
