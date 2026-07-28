"""Tests for SSH fine-grained file operations (transfer.ssh_file_*).

SSH file ops use two mechanisms — ``ssh sh -s`` shell scripts (list/stat/mkdir/
delete) and sftp batch (read/write/rename). Both are mocked at the boundary
(``ssh_mod.run_script`` / ``transfer._run_sftp_batch``); the TSV parsers are
exercised both directly and through the ``ssh_file_*`` wrappers. One CLI test
pins the ssh/bridge dispatch in ``aimux file``.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from aimux import ssh as ssh_mod
from aimux import transfer


def _cp(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# TSV parsers (pure functions)
# ---------------------------------------------------------------------------


def test_parse_stat_output_file():
    assert transfer._parse_stat_output("file\t123\t1700000000\n") == {
        "exists": True,
        "type": "file",
        "size": 123,
        "mtime": 1700000000,
    }


def test_parse_stat_output_dir():
    assert transfer._parse_stat_output("dir\t0\t99\n") == {
        "exists": True,
        "type": "dir",
        "size": 0,
        "mtime": 99,
    }


def test_parse_stat_output_missing():
    assert transfer._parse_stat_output("none\t0\t0\n") == {
        "exists": False,
        "type": "none",
        "size": 0,
        "mtime": 0,
    }


def test_parse_stat_output_empty_stdout():
    assert transfer._parse_stat_output("") == {
        "exists": False,
        "type": "none",
        "size": 0,
        "mtime": 0,
    }


def test_parse_list_output_entries_and_path():
    stdout = "PATH\t/real\nENTRY\tfile\t10\t1700\tname.txt\nENTRY\tdir\t0\t5\tsub\n"
    out = transfer._parse_list_output(stdout)
    assert out["path"] == "/real"
    assert {"name": "name.txt", "type": "file", "size": 10, "mtime": 1700} in out["entries"]
    assert {"name": "sub", "type": "dir", "size": 0, "mtime": 5} in out["entries"]


def test_parse_list_output_err_row_raises():
    with pytest.raises(transfer.TransferError):
        transfer._parse_list_output("ERR\tnot_directory\t/x\n")


# ---------------------------------------------------------------------------
# ssh_file_stat / ssh_file_list (ssh run_script path)
# ---------------------------------------------------------------------------


def test_ssh_file_stat_invokes_run_script_and_parses(monkeypatch):
    captured = {}

    def fake_run_script(name, script, *, timeout=10):
        captured["name"] = name
        captured["script"] = script
        return _cp("file\t42\t7\n")

    monkeypatch.setattr(ssh_mod, "run_script", fake_run_script)
    result = transfer.ssh_file_stat("myhost", "/etc/hosts")
    assert result == {"exists": True, "type": "file", "size": 42, "mtime": 7}
    assert captured["name"] == "myhost"
    assert "/etc/hosts" in captured["script"]


def test_ssh_file_list_recursive_flag_sets_script_var(monkeypatch):
    captured = {}

    def fake_run_script(name, script, *, timeout=10):
        captured["script"] = script
        return _cp("PATH\t/x\nENTRY\tfile\t1\t2\ta.txt\n")

    monkeypatch.setattr(ssh_mod, "run_script", fake_run_script)
    transfer.ssh_file_list("h", "/x", recursive=False)
    assert "RECURSIVE=0" in captured["script"]
    transfer.ssh_file_list("h", "/x", recursive=True)
    assert "RECURSIVE=1" in captured["script"]


def test_ssh_file_list_non_zero_exit_without_entries_raises(monkeypatch):
    monkeypatch.setattr(ssh_mod, "run_script", lambda *a, **k: _cp("", returncode=3, stderr="nope"))
    with pytest.raises(transfer.TransferError):
        transfer.ssh_file_list("h", "/missing")


# ---------------------------------------------------------------------------
# ssh_file_read / ssh_file_write / ssh_file_rename (sftp batch path)
# ---------------------------------------------------------------------------


def test_ssh_file_read_returns_downloaded_bytes(monkeypatch):
    def fake_batch(host, batch, err_cls):
        parts = shlex.split(batch)
        assert parts[0] == "get"
        Path(parts[-1]).write_bytes(b"downloaded")

    monkeypatch.setattr(transfer, "_run_sftp_batch", fake_batch)
    assert transfer.ssh_file_read("h", "/remote/x") == b"downloaded"


def test_ssh_file_read_sftp_failure_wrapped(monkeypatch):
    def boom(host, batch, err_cls):
        raise err_cls(1, "", "no such file", [])

    monkeypatch.setattr(transfer, "_run_sftp_batch", boom)
    with pytest.raises(transfer.TransferError):
        transfer.ssh_file_read("h", "/remote/missing")


def test_ssh_file_write_uploads_bytes_and_mode(monkeypatch):
    captured = {}

    def fake_batch(host, batch, err_cls):
        parts = shlex.split(batch)
        assert parts[0] == "put"
        local, remote = parts[1], parts[2]
        captured["data"] = Path(local).read_bytes()
        captured["remote"] = remote
        captured["mode"] = Path(local).stat().st_mode & 0o777

    monkeypatch.setattr(transfer, "_run_sftp_batch", fake_batch)
    n = transfer.ssh_file_write("h", "/remote/y", b"payload", mode=0o600)
    assert n == len(b"payload")
    assert captured["data"] == b"payload"
    assert captured["remote"] == "/remote/y"
    assert captured["mode"] == 0o600


def test_ssh_file_write_rejects_non_bytes():
    with pytest.raises(transfer.TransferError):
        transfer.ssh_file_write("h", "/x", "not bytes")  # type: ignore[arg-type]


def test_ssh_file_rename_builds_rename_batch(monkeypatch):
    captured = {}

    def fake_batch(host, batch, err_cls):
        parts = shlex.split(batch)
        assert parts[0] == "rename"
        captured["src"] = parts[1]
        captured["dst"] = parts[2]

    monkeypatch.setattr(transfer, "_run_sftp_batch", fake_batch)
    transfer.ssh_file_rename("h", "/a", "/b")
    assert captured == {"src": "/a", "dst": "/b"}


# ---------------------------------------------------------------------------
# ssh_file_mkdir / ssh_file_delete (ssh run_script path)
# ---------------------------------------------------------------------------


def test_ssh_file_mkdir_runs_mkdir_p(monkeypatch):
    captured = {}

    def fake_run_script(name, script, *, timeout=10):
        captured["script"] = script
        return _cp()

    monkeypatch.setattr(ssh_mod, "run_script", fake_run_script)
    transfer.ssh_file_mkdir("h", "/remote/dir")
    assert "mkdir -p" in captured["script"]
    assert "/remote/dir" in captured["script"]


def test_ssh_file_delete_success(monkeypatch):
    monkeypatch.setattr(ssh_mod, "run_script", lambda *a, **k: _cp())
    transfer.ssh_file_delete("h", "/x")  # no raise
    transfer.ssh_file_delete("h", "/x", recursive=True)


def test_ssh_file_delete_directory_without_recursive_raises(monkeypatch):
    monkeypatch.setattr(ssh_mod, "run_script", lambda *a, **k: _cp("ERR\tis_dir\t/x\n", returncode=4))
    with pytest.raises(transfer.TransferError):
        transfer.ssh_file_delete("h", "/x")


def test_ssh_file_delete_failure_wrapped(monkeypatch):
    monkeypatch.setattr(ssh_mod, "run_script", lambda *a, **k: _cp("", returncode=1, stderr="oops"))
    with pytest.raises(transfer.TransferError):
        transfer.ssh_file_delete("h", "/x")


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["ab\0cd", "ab\ncd", "ab\rcd"])
def test_ssh_file_ops_reject_control_chars(path):
    with pytest.raises(transfer.TransferError):
        transfer.ssh_file_stat("h", path)
    with pytest.raises(transfer.TransferError):
        transfer.ssh_file_read("h", path)


# ---------------------------------------------------------------------------
# CLI dispatch (aimux file list -> ssh path)
# ---------------------------------------------------------------------------


def test_cli_file_list_dispatches_to_ssh_and_prints_entries(monkeypatch):
    from typer.testing import CliRunner

    from aimux import cli

    monkeypatch.setattr(cli, "_bridge_remote", lambda remote: (None, None))
    monkeypatch.setattr(cli.ssh_mod, "host_exists", lambda remote: True)
    monkeypatch.setattr(
        cli.transfer,
        "ssh_file_list",
        lambda host, path, recursive=False: {
            "path": "/x",
            "entries": [{"name": "a.txt", "type": "file", "size": 9, "mtime": 0}],
        },
    )
    result = CliRunner().invoke(cli.app, ["file", "list", "myhost", "--path", "/x"])
    assert result.exit_code == 0
    assert "a.txt" in result.output


def test_cli_file_list_unknown_remote_exits_user_error(monkeypatch):
    from typer.testing import CliRunner

    from aimux import cli

    monkeypatch.setattr(cli, "_bridge_remote", lambda remote: (None, None))
    monkeypatch.setattr(cli.ssh_mod, "host_exists", lambda remote: False)
    result = CliRunner().invoke(cli.app, ["file", "list", "ghost", "--path", "/x"])
    assert result.exit_code != 0
