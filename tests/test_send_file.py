from __future__ import annotations

import shutil
import subprocess

import pytest

pytest.importorskip("typer")
from typer.testing import CliRunner

from aimux import ssh as ssh_mod
from aimux import transfer
from aimux.cli import app
from aimux.constants import EXIT_AUTH_REQUIRED, EXIT_OK, EXIT_USER_ERROR


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


def test_send_file_uploads_file_with_sftp_batch(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path, "Host worker\n    HostName localhost\n")
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr(transfer.shutil, "which", lambda name: "/usr/bin/sftp" if name == "sftp" else None)

    local_file = tmp_path / "hello.txt"
    local_file.write_text("hello\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_run(args, *, input, capture_output, text):
        seen["args"] = args
        seen["batch"] = input
        assert capture_output is True
        assert text is True
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transfer.subprocess, "run", fake_run)

    result = CliRunner().invoke(app, ["send_file", str(local_file), "worker", "/tmp/out.txt"])

    assert result.exit_code == EXIT_OK
    assert "uploaded 1 file(s) to worker:/tmp/out.txt" in result.stdout
    assert seen["args"] == ["sftp", "-o", "BatchMode=yes", "-b", "-", "worker"]
    assert seen["batch"] == f'-mkdir "/tmp"\nput "{local_file}" "/tmp/out.txt"\n'


def test_send_file_rejects_missing_host_before_sftp(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path)
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    local_file = tmp_path / "hello.txt"
    local_file.write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(
        transfer,
        "send_path",
        lambda *args, **kwargs: pytest.fail("send_path should not run for a missing host"),
    )

    result = CliRunner().invoke(app, ["send_file", str(local_file), "missing", "/tmp/out.txt"])

    assert result.exit_code == EXIT_USER_ERROR
    assert "host 'missing' not found in ~/.ssh/config" in _combined_output(result)


def test_send_file_reports_auth_required_from_sftp(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path, "Host worker\n    HostName localhost\n")
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr(transfer.shutil, "which", lambda name: "/usr/bin/sftp" if name == "sftp" else None)

    local_file = tmp_path / "hello.txt"
    local_file.write_text("hello\n", encoding="utf-8")

    def fake_run(args, *, input, capture_output, text):
        return subprocess.CompletedProcess(
            args=args,
            returncode=255,
            stdout="",
            stderr="Permission denied (publickey).",
        )

    monkeypatch.setattr(transfer.subprocess, "run", fake_run)

    result = CliRunner().invoke(app, ["send_file", str(local_file), "worker", "/tmp/out.txt"])

    assert result.exit_code == EXIT_AUTH_REQUIRED
    assert "sftp upload to 'worker' failed" in _combined_output(result)


def test_send_file_gitignore_skips_ignored_files(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")

    repo = tmp_path / "repo"
    payload = repo / "payload"
    payload.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    (repo / ".gitignore").write_text("payload/ignored.log\npayload/ignored_dir/\n", encoding="utf-8")
    (payload / "keep.txt").write_text("keep\n", encoding="utf-8")
    (payload / "ignored.log").write_text("ignored\n", encoding="utf-8")
    ignored_dir = payload / "ignored_dir"
    ignored_dir.mkdir()
    (ignored_dir / "nested.txt").write_text("ignored\n", encoding="utf-8")

    plan = transfer.build_send_plan(payload, "/srv/payload", gitignore=True)

    assert [entry.remote for entry in plan.files] == ["/srv/payload/keep.txt"]
    assert plan.skipped_files == 2
    assert "/srv/payload/ignored_dir" not in plan.dirs
