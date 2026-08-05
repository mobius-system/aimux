from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("typer")
from typer.testing import CliRunner

from aimux import ssh as ssh_mod
from aimux import tmux
from aimux.cli import app
from aimux.constants import (
    EXIT_AUTH_REQUIRED,
    EXIT_OK,
    EXIT_SSH_UNREACHABLE,
    EXIT_USER_ERROR,
)


def _write_ssh_config(tmp_path, text: str = ""):
    path = tmp_path / ".ssh" / "config"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def _combined_output(result) -> str:
    try:
        return result.output + result.stderr
    except ValueError:
        return result.output


def test_remote_test_json_reports_auth_required(tmp_path, monkeypatch):
    config = _write_ssh_config(
        tmp_path,
        "Host local-8824\n    HostName localhost\n    Port 8824\n    User root\n",
    )
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr(
        ssh_mod,
        "probe",
        lambda name, timeout: ssh_mod.ProbeResult(name=name, status="auth-required", rtt_ms=17),
    )

    result = CliRunner().invoke(app, ["remote", "test", "local-8824", "--timeout", "2s", "--json"])

    assert result.exit_code == EXIT_AUTH_REQUIRED
    assert json.loads(result.stdout) == {
        "name": "local-8824",
        "status": "auth-required",
        "rtt_ms": 17,
    }


def test_remote_ls_stream_emits_config_before_results_and_uses_completion_order(tmp_path, monkeypatch):
    config = _write_ssh_config(
        tmp_path,
        "Host slow\n    HostName slow.example\n    User root\n"
        "Host fast\n    HostName fast.example\n    User worker\n",
    )
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr("aimux.cli._bridge_client_or_none", lambda: None)

    def probe(name, timeout):
        if name == "slow":
            time.sleep(0.05)
        return ssh_mod.ProbeResult(name=name, status="reachable", rtt_ms=5 if name == "fast" else 50)

    monkeypatch.setattr(ssh_mod, "probe", probe)

    result = CliRunner().invoke(app, ["remote", "ls", "--stream", "--timeout", "2s"])

    assert result.exit_code == EXIT_OK
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert events[0] == {"event": "start", "ssh_count": 2}
    assert [event["remote"]["name"] for event in events[1:3]] == ["slow", "fast"]
    assert all(event["phase"] == "discovered" for event in events[1:3])
    assert all(event["remote"]["status"] == "probing" for event in events[1:3])
    assert [event["remote"]["name"] for event in events[3:5]] == ["fast", "slow"]
    assert all(event["phase"] == "result" for event in events[3:5])
    assert events[-1] == {"event": "done", "count": 2}


def test_remote_test_json_rejects_missing_host(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path)
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)

    result = CliRunner().invoke(app, ["remote", "test", "missing", "--json"])

    assert result.exit_code == EXIT_USER_ERROR
    payload = json.loads(result.stdout)
    assert payload["error"]["kind"] == "host_not_found"
    assert "missing" in payload["error"]["message"]


def test_new_remote_missing_host_fails_before_tmux_create(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path)
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr("aimux.cli._bridge_remote", lambda name: (None, None))
    monkeypatch.setattr(tmux, "has_session", lambda name: False)
    monkeypatch.setattr(
        tmux,
        "new_session",
        lambda *args, **kwargs: pytest.fail("tmux.new_session should not run for a missing remote host"),
    )

    result = CliRunner().invoke(app, ["new", "--remote", "missing", "--name", "task"])

    combined_output = _combined_output(result)
    assert result.exit_code == EXIT_USER_ERROR
    assert "host 'missing' not found in ~/.ssh/config" in combined_output
    assert "tmux failed" not in combined_output


def test_new_remote_reuse_returns_success_even_if_host_config_was_removed(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path)
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr(tmux, "has_session", lambda name: True)
    monkeypatch.setattr(
        tmux,
        "new_session",
        lambda *args, **kwargs: pytest.fail("tmux.new_session should not run when --reuse matches"),
    )

    result = CliRunner().invoke(app, ["new", "--remote", "missing", "--name", "task", "--reuse"])

    assert result.exit_code == EXIT_OK
    assert result.output == ""


def test_new_remote_session_that_exits_immediately_reports_ssh_error(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path, "Host bad\n    HostName missing.invalid\n")
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr(ssh_mod, "is_resolvable", lambda host: True)
    monkeypatch.setattr(tmux, "has_session", lambda name: False)
    monkeypatch.setattr(tmux, "new_session", lambda *args, **kwargs: None)

    def fail_set_option(*args, **kwargs):
        raise tmux.TmuxError(1, "no server running on /tmp/tmux-test/aimux", ["tmux"])

    monkeypatch.setattr(tmux, "set_option", fail_set_option)

    result = CliRunner().invoke(app, ["new", "--remote", "bad", "--name", "task"])

    combined_output = _combined_output(result)
    assert result.exit_code == EXIT_SSH_UNREACHABLE
    assert "remote session 'bad'/'task' exited before aimux could initialize it" in combined_output
    assert "aimux remote test bad" in combined_output
    assert "tmux failed" not in combined_output


def test_remote_add_appends_localhost_8824_after_reachable_probe(tmp_path, monkeypatch):
    config = tmp_path / ".ssh" / "config"
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr(
        ssh_mod,
        "probe_raw",
        lambda host, port, user, identity, timeout: ssh_mod.ProbeResult(
            name=host,
            status="reachable",
            rtt_ms=5,
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "remote",
            "add",
            "--host",
            "localhost",
            "--port",
            "8824",
            "--user",
            "root",
            "--name",
            "local-8824",
            "--timeout",
            "2s",
        ],
    )

    assert result.exit_code == EXIT_OK
    assert "local-8824\treachable\trtt=5ms" in result.stdout
    content = config.read_text(encoding="utf-8")
    assert "Host local-8824" in content
    assert "    HostName localhost" in content
    assert "    Port 8824" in content
    assert "    User root" in content


def test_remote_add_does_not_append_when_probe_needs_auth(tmp_path, monkeypatch):
    config = tmp_path / ".ssh" / "config"
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr(
        ssh_mod,
        "probe_raw",
        lambda host, port, user, identity, timeout: ssh_mod.ProbeResult(
            name=host,
            status="auth-required",
            rtt_ms=8,
            stderr="Permission denied (publickey,password).",
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "remote",
            "add",
            "--host",
            "localhost",
            "--port",
            "8824",
            "--user",
            "root",
            "--name",
            "local-8824",
        ],
    )

    assert result.exit_code == EXIT_AUTH_REQUIRED
    combined_output = _combined_output(result)
    assert "auth-required" in combined_output
    assert not config.exists()
