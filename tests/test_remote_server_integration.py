from __future__ import annotations

import os
import subprocess

import pytest

from aimux import ssh as ssh_mod


@pytest.mark.integration
def test_documented_localhost_8824_root_server_is_probeable(tmp_path):
    host = os.environ.get("AIMUX_TEST_SSH_HOST", "localhost")
    port = int(os.environ.get("AIMUX_TEST_SSH_PORT", "8824"))
    user = os.environ.get("AIMUX_TEST_SSH_USER", "root")
    timeout = int(os.environ.get("AIMUX_TEST_SSH_TIMEOUT", "2"))
    known_hosts = tmp_path / "known_hosts"

    args = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-p",
        str(port),
        f"{user}@{host}",
        "true",
    ]

    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout + 5)
    except FileNotFoundError:
        pytest.skip("ssh client is not installed")
    except subprocess.TimeoutExpired:
        pytest.skip(f"{user}@{host}:{port} did not respond before the test timeout")

    status = ssh_mod._classify(result.returncode, result.stderr)
    if status == "unreachable":
        pytest.skip(f"{user}@{host}:{port} is not reachable in this environment")

    assert status in {"reachable", "auth-required"}, result.stderr
