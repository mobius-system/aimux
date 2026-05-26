from __future__ import annotations

import json

import pytest

pytest.importorskip("typer")
from typer.testing import CliRunner

from aimux import ssh as ssh_mod
from aimux.cli import app
from aimux.constants import EXIT_AUTH_REQUIRED, EXIT_OK, EXIT_USER_ERROR


def _write_ssh_config(tmp_path, text: str = ""):
    path = tmp_path / ".ssh" / "config"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


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


def test_remote_test_json_rejects_missing_host(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path)
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)

    result = CliRunner().invoke(app, ["remote", "test", "missing", "--json"])

    assert result.exit_code == EXIT_USER_ERROR
    payload = json.loads(result.stdout)
    assert payload["error"]["kind"] == "host_not_found"
    assert "missing" in payload["error"]["message"]


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
    combined_output = result.output + getattr(result, "stderr", "")
    assert "auth-required" in combined_output
    assert not config.exists()
