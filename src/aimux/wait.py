"""wait-last-command-complete: poll pane_current_command for completion."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import tmux
from .constants import SHELLS


_DURATION_RE = re.compile(r"^(\d+)([smh])$")


def parse_duration(s: str) -> float:
    m = _DURATION_RE.match(s)
    if not m:
        raise ValueError(f"invalid duration {s!r}; expected <int>s|m|h, e.g. 30s, 5m, 2h")
    n, u = int(m.group(1)), m.group(2)
    return n * {"s": 1, "m": 60, "h": 3600}[u]


@dataclass
class WaitResult:
    completed: bool
    elapsed_ms: int
    session_gone: bool


def wait(session_name: str, timeout_s: float, poll_s: float = 2.0) -> WaitResult:
    """Poll until pane_current_command is in SHELLS, or timeout, or session gone."""
    start = time.monotonic()
    deadline = start + timeout_s
    while True:
        cmds = tmux.list_panes_current_command(session_name)
        if cmds is None:
            return WaitResult(False, int((time.monotonic() - start) * 1000), session_gone=True)
        if cmds and all(c.lower() in SHELLS for c in cmds):
            return WaitResult(True, int((time.monotonic() - start) * 1000), session_gone=False)
        now = time.monotonic()
        if now >= deadline:
            return WaitResult(False, int((now - start) * 1000), session_gone=False)
        time.sleep(min(poll_s, max(0.05, deadline - now)))
