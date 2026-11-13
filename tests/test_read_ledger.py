"""The read ledger that gates remote_edit / remote_write / patch behind remote_read.

The ledger lives on the bridge client machine only; these tests exercise the
client functions directly (no bridge, no network).
"""
from __future__ import annotations

import base64
import json
import time

import pytest

from aimux import bridge_client
from aimux.bridge import BridgeError


@pytest.fixture
def ledger_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def _runtime():
    """A bare runtime: _file_read/_file_write only need _file_path."""
    return object.__new__(bridge_client.ReverseClientRuntime)


def _write_payload(path, data: bytes, *, session="s1", policy="require"):
    payload = {"path": str(path), "content_b64": base64.b64encode(data).decode("ascii")}
    if session:
        payload["session"] = session
    if policy:
        payload["read_policy"] = policy
    return payload


def _read_payload(path, *, session="s1", policy="internal"):
    payload = {"path": str(path)}
    if session:
        payload["session"] = session
    if policy:
        payload["read_policy"] = policy
    return payload


def test_write_over_existing_file_requires_a_prior_read(ledger_home, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")

    with pytest.raises(BridgeError) as excinfo:
        _runtime()._file_write(_write_payload(target, b"two\n"))

    assert excinfo.value.kind == "read_required"
    assert target.read_text(encoding="utf-8") == "one\n"  # untouched


def test_read_record_policy_grants_the_edit(ledger_home, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")

    _runtime()._file_read(_read_payload(target, policy="record"))
    _runtime()._file_write(_write_payload(target, b"two\n"))

    assert target.read_text(encoding="utf-8") == "two\n"
    ledger = json.loads((ledger_home / ".aimux" / "read-ledger" / "s1.json").read_text())
    entry = ledger["reads"][0]
    assert entry["size_when_read"] == len("two\n")  # refreshed by the write


def test_internal_read_does_not_grant_writing(ledger_home, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")

    _runtime()._file_read(_read_payload(target, policy="internal"))

    with pytest.raises(BridgeError) as excinfo:
        _runtime()._file_write(_write_payload(target, b"two\n"))
    assert excinfo.value.kind == "read_required"


def test_external_change_after_read_is_stale(ledger_home, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")

    _runtime()._file_read(_read_payload(target, policy="record"))
    target.write_text("one\ntwo\n", encoding="utf-8")  # someone else edited it

    with pytest.raises(BridgeError) as excinfo:
        _runtime()._file_write(_write_payload(target, b"three\n"))
    assert excinfo.value.kind == "stale_read"


def test_new_file_needs_no_prior_read(ledger_home, tmp_path):
    target = tmp_path / "new.py"

    _runtime()._file_write(_write_payload(target, b"print(1)\n"))

    assert target.read_text(encoding="utf-8") == "print(1)\n"
    # The write registered the file, so a follow-up edit needs no remote_read.
    _runtime()._file_write(_write_payload(target, b"print(2)\n"))
    assert target.read_text(encoding="utf-8") == "print(2)\n"


def test_delete_requires_a_prior_read(ledger_home, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")

    with pytest.raises(BridgeError) as excinfo:
        _runtime()._file_delete({"path": str(target), "session": "s1", "read_policy": "require"})
    assert excinfo.value.kind == "read_required"
    assert target.exists()

    _runtime()._file_read(_read_payload(target, policy="record"))
    _runtime()._file_delete({"path": str(target), "session": "s1", "read_policy": "require"})
    assert not target.exists()


def test_sessions_are_isolated(ledger_home, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")

    _runtime()._file_read(_read_payload(target, session="s1", policy="record"))

    with pytest.raises(BridgeError):
        _runtime()._file_write(_write_payload(target, b"two\n", session="s2"))


def test_ledger_keeps_at_most_127_entries(ledger_home, tmp_path):
    now = time.time()
    entries = [
        {"file_fp": f"/f{i}", "read_at": now - i, "mtime_when_read_ns": 1, "size_when_read": 1}
        for i in range(200)
    ]
    pruned = bridge_client._ledger_prune(entries, now)
    assert len(pruned) == bridge_client.READ_LEDGER_MAX_ENTRIES == 127
    # Oldest dropped, newest kept.
    assert pruned[-1]["file_fp"] == "/f0"


def test_ledger_prunes_entries_older_than_a_day(ledger_home, tmp_path):
    now = time.time()
    entries = [
        {"file_fp": "/old", "read_at": now - 2 * 86400, "mtime_when_read_ns": 1, "size_when_read": 1},
        {"file_fp": "/fresh", "read_at": now - 10, "mtime_when_read_ns": 1, "size_when_read": 1},
    ]
    pruned = bridge_client._ledger_prune(entries, now)
    assert [entry["file_fp"] for entry in pruned] == ["/fresh"]


def test_stale_session_ledger_file_is_removed(ledger_home):
    directory = ledger_home / ".aimux" / "read-ledger"
    directory.mkdir(parents=True)
    stale = directory / "old-session.json"
    stale.write_text('{"version": 1, "reads": []}', encoding="utf-8")
    two_days_ago = time.time() - 2 * 86400
    import os
    os.utime(stale, (two_days_ago, two_days_ago))

    bridge_client._ledger_cleanup_sessions(time.time())

    assert not stale.exists()


def test_corrupt_ledger_does_not_unlock_writes(ledger_home, tmp_path):
    directory = ledger_home / ".aimux" / "read-ledger"
    directory.mkdir(parents=True)
    (directory / "s1.json").write_text("{not json", encoding="utf-8")
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")

    with pytest.raises(BridgeError) as excinfo:
        _runtime()._file_write(_write_payload(target, b"two\n"))
    assert excinfo.value.kind == "read_required"


def test_non_text_files_are_not_recorded(ledger_home, tmp_path):
    target = tmp_path / "blob.bin"
    target.write_bytes(b"\x00\x01\x02\x03")

    with pytest.raises(BridgeError) as excinfo:
        _runtime()._file_read(_read_payload(target, policy="record"))

    assert excinfo.value.kind == "not_text"
    ledger = ledger_home / ".aimux" / "read-ledger" / "s1.json"
    assert not ledger.exists()


def test_read_policy_record_requires_a_session(ledger_home, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")

    with pytest.raises(BridgeError) as excinfo:
        _runtime()._file_read(_read_payload(target, session="", policy="record"))
    assert excinfo.value.kind == "bad_request"


def test_legacy_calls_without_session_keep_working(ledger_home, tmp_path):
    """CLI and internal callers do not pass a session and are not gated."""
    target = tmp_path / "app.py"
    target.write_text("one\n", encoding="utf-8")

    _runtime()._file_write({"path": str(target), "content_b64": base64.b64encode(b"two\n").decode()})

    assert target.read_text(encoding="utf-8") == "two\n"
