from __future__ import annotations

import shlex
import subprocess
from textwrap import dedent

import pytest

from aimux import ssh as ssh_mod


def _write_ssh_config(tmp_path, text: str):
    path = tmp_path / ".ssh" / "config"
    path.parent.mkdir(parents=True)
    path.write_text(dedent(text).lstrip(), encoding="utf-8")
    return path


def test_list_hosts_reads_includes_and_localhost_8824_config(tmp_path, monkeypatch):
    config = _write_ssh_config(
        tmp_path,
        """
        Include conf.d/*.conf

        Host wildcard-*
            HostName ignored.example
            Port 2200
        """,
    )
    include_dir = config.parent / "conf.d"
    include_dir.mkdir()
    (include_dir / "servers.conf").write_text(
        dedent(
            """
            Host local-8824
                HostName localhost
                Port 8824
                User root

            Host bad-port
                HostName localhost
                Port not-a-number
                User root
            """
        ).lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)

    hosts = {host.name: host for host in ssh_mod.list_hosts()}

    assert "wildcard-*" not in hosts
    assert hosts["local-8824"] == ssh_mod.SshHost(
        name="local-8824",
        hostname="localhost",
        user="root",
        port=8824,
    )
    assert hosts["bad-port"].port == 22


def test_append_host_writes_localhost_8824_block_and_backup(tmp_path, monkeypatch):
    config = _write_ssh_config(
        tmp_path,
        """
        Host existing
            HostName example.com
            User deploy
        """,
    )
    original = config.read_text(encoding="utf-8")
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)

    backup = ssh_mod.append_host(
        name="local-8824",
        host="localhost",
        port=8824,
        user="root",
        identity="~/.ssh/id_local_8824",
    )

    assert backup.exists()
    assert backup.read_text(encoding="utf-8") == original
    content = config.read_text(encoding="utf-8")
    assert "Host local-8824" in content
    assert "    HostName localhost" in content
    assert "    Port 8824" in content
    assert "    User root" in content
    assert "    IdentityFile ~/.ssh/id_local_8824" in content

    with pytest.raises(FileExistsError):
        ssh_mod.append_host(name="local-8824", host="localhost", port=8824, user="root")


def test_probe_raw_builds_batchmode_ssh_for_localhost_8824(monkeypatch):
    seen: dict[str, object] = {}

    def fake_run(args, *, capture_output, text, timeout):
        seen["args"] = args
        seen["capture_output"] = capture_output
        seen["text"] = text
        seen["timeout"] = timeout
        return subprocess.CompletedProcess(
            args=args,
            returncode=255,
            stdout="",
            stderr="root@localhost: Permission denied (publickey,password).",
        )

    monkeypatch.setattr(ssh_mod.subprocess, "run", fake_run)

    result = ssh_mod.probe_raw(
        "localhost",
        8824,
        "root",
        identity="/tmp/test-key",
        timeout=3,
    )

    assert result.status == "auth-required"
    assert result.rtt_ms is not None
    assert seen["capture_output"] is True
    assert seen["text"] is True
    assert seen["timeout"] == 8
    args = seen["args"]
    assert args[:7] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=3",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    assert args[args.index("-p") + 1] == "8824"
    assert args[args.index("-i") + 1] == "/tmp/test-key"
    assert args[-2:] == ["root@localhost", "true"]


def test_build_remote_launch_quotes_cwd_for_remote_shell():
    launch = ssh_mod.build_remote_launch("local-8824", cwd="/srv/my project", cmd=None)

    argv = shlex.split(launch)

    assert argv == [
        "ssh",
        "-t",
        "local-8824",
        "cd '/srv/my project' && exec $SHELL -l",
    ]


def test_hardware_info_parses_remote_probe_output(monkeypatch):
    def fake_run(args, *, input, capture_output, text, timeout):
        assert args[:3] == ["ssh", "-o", "BatchMode=yes"]
        assert args[-3:] == ["local-8824", "sh", "-s"]
        assert "nvidia-smi" in input
        assert capture_output is True
        assert text is True
        assert timeout == 12
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="gpu_count=2\ngpu_model=NVIDIA A100\ncpu_count=64\nmem_kb=131072\n",
            stderr="",
        )

    monkeypatch.setattr(ssh_mod.subprocess, "run", fake_run)

    info = ssh_mod.hardware_info("local-8824", timeout=2)

    assert info == ssh_mod.HardwareInfo(
        gpu_count=2,
        gpu_model="NVIDIA A100",
        cpu_count=64,
        mem_total_kb=131072,
    )
