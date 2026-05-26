"""Hardcoded constants. aimux does not read a config file."""
from __future__ import annotations

import importlib.resources
from pathlib import Path

SOCKET_NAME = "aimux"

# user-option keys
OPT_LOCATION = "@aimux-location"
OPT_HOST = "@aimux-host"
OPT_CREATED_AT = "@aimux-created-at"
OPT_CREATED_BY = "@aimux-created-by"
OPT_OUTPUT_SYNC_FILE = "@aimux-output-sync-file"

# Known shell names (case-insensitive). Used by wait-last-command-complete
# to detect "pane is back at a shell prompt".
SHELLS = {"bash", "zsh", "sh", "fish", "csh", "tcsh", "ksh", "dash", "ash"}

# Exit codes (semantic; see design.md §5)
EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_SSH_UNREACHABLE = 2
EXIT_SAFETY_DENIED = 3
EXIT_AUTH_REQUIRED = 4
EXIT_TIMEOUT = 124


def aimux_conf_path() -> Path:
    """Absolute path to the bundled aimux.conf resource."""
    return Path(str(importlib.resources.files("aimux") / "aimux.conf"))
