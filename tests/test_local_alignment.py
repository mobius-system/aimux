"""Tests for the standalone local OODA GUI client.

The module under test must not depend on aimux internals, so these tests
replace the Rust helper with a tiny Python process speaking the same
stdin/stdout JSON-lines protocol, and assert both behaviour and the absence of
intra-package imports.
"""
from __future__ import annotations

import ast
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aimux import local_alignment
from aimux.local_alignment import (
    LocalGuiError,
    fast_ooda_gui_act,
    fast_ooda_gui_diagnostics,
    fast_ooda_gui_focus_window,
    fast_ooda_gui_list_windows,
    fast_ooda_gui_look,
    fast_ooda_gui_read_text,
    fast_ooda_gui_record_window_mp4,
    fast_ooda_gui_wait_for,
)

MOCK_HELPER = r"""
import base64, json, os, pathlib, sys

log_path = os.environ.get("FAST_ODA_MOCK_LOG")
mock_dir = pathlib.Path(os.environ["FAST_ODA_MOCK_DIR"])
status_calls = 0

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    cmd = req.get("cmd")
    args = req.get("args") or {}
    if log_path:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"cmd": cmd, "args": args}, ensure_ascii=False) + "\n")

    def ok(result):
        return {"protocolVersion": 6, "id": req.get("id"), "ok": True, "result": result}

    def fail(message, code):
        return {"protocolVersion": 6, "id": req.get("id"), "ok": False,
                "error": {"message": message, "code": code}}

    if args.get("pid") == 999:
        resp = fail("mock failure", "target_not_found")
    elif cmd == "diagnostics":
        resp = ok({"protocolVersion": 6, "os": "windows", "arch": "x86_64",
                   "pid": os.getpid(), "accessibility": True, "screenRecording": True})
    elif cmd == "listRoots":
        resp = ok({"roots": [{"rootRef": "@w1", "windowId": 1, "pid": 42,
                              "title": "mock window", "kind": "window",
                              "processName": "mock.exe", "isFocused": True,
                              "isMain": True, "isMinimized": False,
                              "framePoints": {"x": 0, "y": 0, "w": 10, "h": 10}}]})
    elif cmd == "look":
        image = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
        resp = ok({"lookId": "look_1", "window": {"title": "mock window"},
                   "outline": [{"ref": "@e1", "role": "button", "name": "OK"}],
                   "timings": {"totalMs": 1},
                   "image": {"jpegBase64": image, "width": 10, "height": 10}})
    elif cmd == "act":
        resp = ok({"outcome": "worked",
                   "performed": {"grounding": "description", "delivery": "ax"}})
    elif cmd == "axReadText":
        resp = ok({"text": "hello", "offset": args.get("offset", 0),
                   "limit": args.get("limit", 4096), "totalChars": 5, "hasMore": False})
    elif cmd == "axWaitFor":
        resp = ok({"found": True, "matches": 1})
    elif cmd == "focusWindow":
        resp = ok({"focused": True})
    elif cmd == "recordStart":
        path = mock_dir / "rec.mp4"
        path.write_bytes(b"mp4data")
        resp = ok({"recordingId": "rec1", "path": str(path),
                   "width": 1280, "height": 720, "fps": args.get("fps"),
                   "audio": args.get("audio"), "hasAudio": True,
                   "maxDimension": args.get("maxDimension")})
    elif cmd == "recordStatus":
        status_calls += 1
        resp = ok({"active": status_calls < 1})
    elif cmd == "recordStop":
        path = mock_dir / "rec.mp4"
        resp = ok({"path": str(path), "bytes": path.stat().st_size,
                   "durationMs": 1000, "width": 1280, "height": 720, "fps": 10,
                   "audio": "system", "hasAudio": True, "videoFrames": 10,
                   "audioFrames": 10})
    else:
        resp = ok({"echo": cmd})
    sys.stdout.write(json.dumps(resp) + "\n")
    sys.stdout.flush()
"""

MODULE_PATH = Path(local_alignment.__file__)


@pytest.fixture()
def mock_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "mock-helper"
    script.write_text("#!/usr/bin/env python3\n" + MOCK_HELPER, encoding="utf-8")
    script.chmod(0o755)
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("AIMUX_GUI_HELPER_PATH", str(script))
    monkeypatch.setenv("FAST_ODA_MOCK_LOG", str(log))
    monkeypatch.setenv("FAST_ODA_MOCK_DIR", str(tmp_path))
    local_alignment.configure()
    try:
        yield SimpleNamespace(script=script, log=log, tmp=tmp_path)
    finally:
        local_alignment.close()
        local_alignment.configure()


def _requests(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _last(log: Path) -> dict:
    entries = _requests(log)
    assert entries, "mock helper received no requests"
    return entries[-1]


def test_module_imports_nothing_from_aimux() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not [name for name in imported if name == "aimux" or name.startswith("aimux.")]


def test_diagnostics(mock_helper) -> None:
    result = fast_ooda_gui_diagnostics()
    assert result["protocolVersion"] == local_alignment.PROTOCOL_VERSION
    assert result["os"] == "windows"
    assert _last(mock_helper.log)["cmd"] == "diagnostics"


def test_list_windows_returns_full_roots(mock_helper) -> None:
    result = fast_ooda_gui_list_windows()
    assert result["roots"][0]["windowId"] == 1
    # Fields the MCP tool trims away are still present locally.
    assert result["roots"][0]["framePoints"]["w"] == 10
    assert _last(mock_helper.log)["args"] == {}


def test_list_windows_pid_filter(mock_helper) -> None:
    fast_ooda_gui_list_windows(pid=42)
    assert _last(mock_helper.log)["args"] == {"pid": 42}


def test_look_saves_png_and_translates_arguments(mock_helper) -> None:
    target = mock_helper.tmp / "shots" / "window.png"
    result = fast_ooda_gui_look(
        "@w1", include_image=True, max_dimension=512, save_image=target
    )
    assert result["lookId"] == "look_1"
    assert result["image_path"] == str(target)
    assert target.read_bytes() == b"\x89PNG\r\n\x1a\nfake"
    request = _last(mock_helper.log)
    assert request["cmd"] == "look"
    assert request["args"] == {
        "rootRef": "@w1", "includeImage": True, "maxDimension": 512
    }


def test_look_outline_budget_is_opt_in(mock_helper) -> None:
    result = fast_ooda_gui_look("@w1")
    assert isinstance(result["outline"], list)
    assert "estimated_tokens" not in result

    truncated = fast_ooda_gui_look("@w1", max_output_tokens=1)
    assert truncated["outline_truncated"] is True
    assert truncated["outline"].endswith("...[truncated]...")
    assert truncated["estimated_tokens"] > 1


def test_look_requires_target(mock_helper) -> None:
    with pytest.raises(LocalGuiError) as excinfo:
        fast_ooda_gui_look()
    assert excinfo.value.code == "invalid_request"


def test_act_packs_target_and_params(mock_helper) -> None:
    fast_ooda_gui_act(
        "look_1",
        "keypress",
        ref="@e1",
        policy="foreground",
        keys=["ctrl", "c"],
        keep_pressdown_ms=100,
        defer_root_delta=True,
    )
    args = _last(mock_helper.log)["args"]
    assert args["lookId"] == "look_1"
    assert args["action"] == "keypress"
    assert args["policy"] == "foreground"
    assert args["target"] == {"ref": "@e1"}
    assert args["params"] == {"keys": ["ctrl", "c"], "keepPressdownMs": 100}
    assert args["deferRootDelta"] is True


def test_act_coordinate_target(mock_helper) -> None:
    fast_ooda_gui_act("look_1", "click", x=12, y=34, policy="foreground")
    args = _last(mock_helper.log)["args"]
    assert args["target"] == {"x": 12.0, "y": 34.0}


def test_act_requires_target(mock_helper) -> None:
    with pytest.raises(LocalGuiError) as excinfo:
        fast_ooda_gui_act("look_1", "press")
    assert excinfo.value.code == "invalid_request"


def test_read_text_defaults(mock_helper) -> None:
    result = fast_ooda_gui_read_text("look_1", "@e1")
    assert result["text"] == "hello"
    assert _last(mock_helper.log)["args"] == {
        "lookId": "look_1", "elementRef": "@e1", "offset": 0, "limit": 4096
    }


def test_wait_for_translates_and_scopes(mock_helper) -> None:
    fast_ooda_gui_wait_for(
        text="OK", timeout_ms=1500, scope_exact=True,
        look_id="look_1", scope_ref="@e1",
    )
    args = _last(mock_helper.log)["args"]
    assert args == {
        "gone": False,
        "timeoutMs": 1500,
        "scopeExact": True,
        "text": "OK",
        "lookId": "look_1",
        "scopeRef": "@e1",
    }


def test_wait_for_requires_a_matcher(mock_helper) -> None:
    with pytest.raises(LocalGuiError) as excinfo:
        fast_ooda_gui_wait_for(timeout_ms=100)
    assert excinfo.value.code == "invalid_request"


def test_focus_window(mock_helper) -> None:
    result = fast_ooda_gui_focus_window("@w1")
    assert result["focused"] is True
    assert _last(mock_helper.log)["args"] == {"rootRef": "@w1"}


def test_record_copies_to_local_path(mock_helper) -> None:
    destination = mock_helper.tmp / "out" / "clip.mp4"
    result = fast_ooda_gui_record_window_mp4(
        "@w1", duration_ms=1000, record_fps=15, local_path=destination,
        poll_seconds=0.01,
    )
    assert destination.read_bytes() == b"mp4data"
    assert result["video_path"] == str(destination)
    assert result["size"] == 7
    assert result["recording_id"] == "rec1"
    assert result["video_frames"] == 10

    start = next(r for r in _requests(mock_helper.log) if r["cmd"] == "recordStart")
    assert start["args"]["fps"] == 15
    assert start["args"]["maxDurationMs"] == 1000


def test_record_fps_alias(mock_helper) -> None:
    fast_ooda_gui_record_window_mp4(
        "@w1", duration_ms=1000, fps=7, poll_seconds=0.01
    )
    start = next(r for r in _requests(mock_helper.log) if r["cmd"] == "recordStart")
    assert start["args"]["fps"] == 7


def test_record_local_path_exists_without_overwrite(mock_helper) -> None:
    existing = mock_helper.tmp / "existing.mp4"
    existing.write_bytes(b"old")
    with pytest.raises(LocalGuiError) as excinfo:
        fast_ooda_gui_record_window_mp4(
            "@w1", duration_ms=1000, local_path=existing, poll_seconds=0.01
        )
    assert excinfo.value.code == "file_exists"


def test_helper_error_code_propagates(mock_helper) -> None:
    with pytest.raises(LocalGuiError) as excinfo:
        fast_ooda_gui_list_windows(pid=999)
    assert excinfo.value.code == "target_not_found"
    assert "mock failure" in str(excinfo.value)


def test_missing_helper_is_reported(tmp_path: Path) -> None:
    local_alignment.configure(helper_path=tmp_path / "absent.exe")
    try:
        with pytest.raises(LocalGuiError) as excinfo:
            fast_ooda_gui_diagnostics()
        assert excinfo.value.code == "helper_not_found"
    finally:
        local_alignment.configure()


def test_close_allows_a_fresh_helper(mock_helper) -> None:
    fast_ooda_gui_diagnostics()
    local_alignment.close()
    fast_ooda_gui_diagnostics()
    # Each helper process sends one startup probe plus the explicit call.
    assert len([r for r in _requests(mock_helper.log) if r["cmd"] == "diagnostics"]) == 4
