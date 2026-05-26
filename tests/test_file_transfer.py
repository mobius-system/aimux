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


def test_send_files_rejects_missing_host_before_sftp(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path)
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    local_file = tmp_path / "hello.txt"
    local_file.write_text("hello\n", encoding="utf-8")
    monkeypatch.setattr(
        transfer,
        "send_paths",
        lambda *args, **kwargs: pytest.fail("send_paths should not run for a missing host"),
    )

    result = CliRunner().invoke(app, ["send_files", "missing", "/tmp/out", str(local_file)])

    assert result.exit_code == EXIT_USER_ERROR
    assert "host 'missing' not found in ~/.ssh/config" in _combined_output(result)


def test_send_files_reports_auth_required_from_sftp(tmp_path, monkeypatch):
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

    result = CliRunner().invoke(app, ["send_files", "worker", "/tmp/out", str(local_file)])

    assert result.exit_code == EXIT_AUTH_REQUIRED
    assert "sftp upload to 'worker' failed" in _combined_output(result)


def test_send_files_gitignore_skips_ignored_files(tmp_path):
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

    plan = transfer.build_send_files_plan([payload], "/srv", gitignore=True)

    assert [entry.remote for entry in plan.files] == ["/srv/payload/keep.txt"]
    assert plan.skipped_files == 2
    assert "/srv/payload/ignored_dir" not in plan.dirs


def test_send_files_uploads_multiple_paths_to_remote_directory(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path, "Host worker\n    HostName localhost\n")
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr(transfer.shutil, "which", lambda name: "/usr/bin/sftp" if name == "sftp" else None)

    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a\n", encoding="utf-8")
    second.write_text("b\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def fake_run(args, *, input, capture_output, text):
        seen["args"] = args
        seen["batch"] = input
        assert capture_output is True
        assert text is True
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transfer.subprocess, "run", fake_run)

    result = CliRunner().invoke(app, ["send_files", "worker", "/tmp/out", str(first), str(second)])

    assert result.exit_code == EXIT_OK
    assert "uploaded 2 file(s) to worker:/tmp/out" in result.stdout
    assert seen["args"] == ["sftp", "-o", "BatchMode=yes", "-b", "-", "worker"]
    assert seen["batch"] == (
        f'-mkdir "/tmp"\n'
        f'-mkdir "/tmp/out"\n'
        f'put "{first}" "/tmp/out/a.txt"\n'
        f'put "{second}" "/tmp/out/b.txt"\n'
    )


def test_send_files_maps_directories_under_their_basename(tmp_path):
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    (project / "root.txt").write_text("root\n", encoding="utf-8")
    (nested / "child.txt").write_text("child\n", encoding="utf-8")

    plan = transfer.build_send_files_plan([project], "/srv/uploads")

    assert [entry.remote for entry in plan.files] == [
        "/srv/uploads/project/nested/child.txt",
        "/srv/uploads/project/root.txt",
    ]
    assert "/srv/uploads/project" in plan.dirs
    assert "/srv/uploads/project/nested" in plan.dirs


def test_send_files_rejects_duplicate_remote_destinations(tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    left.mkdir()
    right.mkdir()
    (left / "same.txt").write_text("left\n", encoding="utf-8")
    (right / "same.txt").write_text("right\n", encoding="utf-8")

    with pytest.raises(transfer.TransferError, match="multiple local files map"):
        transfer.build_send_files_plan([left / "same.txt", right / "same.txt"], "/srv/uploads")


def test_get_files_downloads_multiple_remote_paths_to_local_directory(tmp_path, monkeypatch):
    config = _write_ssh_config(tmp_path, "Host worker\n    HostName localhost\n")
    monkeypatch.setattr(ssh_mod, "_CONFIG_PATH", config)
    monkeypatch.setattr(transfer.shutil, "which", lambda name: "/usr/bin/sftp" if name == "sftp" else None)

    destination = tmp_path / "downloads"
    seen: dict[str, object] = {}

    def fake_run(args, *, input, capture_output, text):
        seen["args"] = args
        seen["batch"] = input
        assert capture_output is True
        assert text is True
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transfer.subprocess, "run", fake_run)

    result = CliRunner().invoke(
        app,
        ["get_files", "worker", str(destination), "/var/log/app.log", "/srv/data/"],
    )

    assert result.exit_code == EXIT_OK
    assert destination.is_dir()
    assert f"downloaded 2 path(s) from worker to {destination}" in result.stdout
    assert seen["args"] == ["sftp", "-o", "BatchMode=yes", "-b", "-", "worker"]
    assert seen["batch"] == (
        f'get -r "/var/log/app.log" "{destination}/app.log"\n'
        f'get -r "/srv/data/" "{destination}/data"\n'
    )


def test_get_files_rejects_local_destination_file(tmp_path):
    destination = tmp_path / "download"
    destination.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(transfer.TransferError, match="local destination is not a directory"):
        transfer.build_get_files_plan(["/tmp/file.txt"], destination)
