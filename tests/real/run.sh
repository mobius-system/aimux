#!/usr/bin/env bash
# Container entrypoint: install aimux editable from the mounted source, then
# run pytest. Passes through extra args (e.g. "-k exec" to filter).
# All setup lives here (not in the Dockerfile) because the host's buildkit
# runc intermittently fails RUN steps while `docker run` is healthy.
set -euo pipefail
cd /opt/aimux

# A HOME is required: the bridge broker writes ~/.aimux/bridge/runtime.json
# and the reverse-connect client + MCP server read it back. The default root
# home works, but make it explicit so a mismatched HOME never splits state.
export HOME="${HOME:-/root}"

python3 -m pip install --upgrade pip --quiet
pip install -e .[test] --quiet

# NO exec: python must NOT become PID 1. mcp_server's parent-death TOCTOU
# guard exits 127 when its parent's pid is 1, so a PID-1 pytest instantly
# kills every `aimux mcp serve` child it spawns.
python3 -m pytest "$@"
