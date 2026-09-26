from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True, scope="session")
def _never_exit_the_runner():
    """Keep a watchdog from taking the test runner down with it.

    The client's watchdog ends the process with ``os._exit(0)``. Its thread
    outlives the test that started it, so one firing between tests would stop
    the run dead -- with a success status and no summary. Tests that assert on
    the exit replace this themselves.
    """
    original = os._exit
    os._exit = lambda code: None  # noqa: SLF001 - deliberate, see docstring
    try:
        yield
    finally:
        os._exit = original

