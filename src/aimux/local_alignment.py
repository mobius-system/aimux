"""Fast local OODA primitives for computer use.

This module is part of the aimux distribution, but it is functionally
standalone: it imports nothing from the rest of the package and knows nothing
about the MCP server, the bridge broker, the reverse client, or any aimux
runtime state. Its only dependency is the native GUI helper binary, which it
starts directly and drives over stdin/stdout JSON lines.

Why it exists
-------------
Every ``remote_gui_*`` MCP tool pays for a five-hop round trip (MCP server ->
bridge client -> broker -> reverse client -> helper). When a script already
runs *on the machine that owns the desktop session*, that chain is pure
overhead. Importing this module and calling ``fast_ooda_gui_*`` reaches the
same helper commands in one local process hop, which is what a rule-based
script needs: observe, compute, act, with millisecond latency.

Most functions mirror the MCP tools one-to-one; the fast screenshot command is
local-only and has no MCP counterpart:

========================================  =============================
local function                            helper command
========================================  =============================
``fast_ooda_gui_diagnostics``             ``diagnostics``
``fast_ooda_gui_list_windows``            ``listRoots``
``fast_ooda_gui_look``                    ``look``
``fast_ooda_gui_look_fast_screenshot``    ``lookFastScreenshot``
``fast_ooda_gui_act``                     ``act``
``fast_ooda_gui_read_text``               ``axReadText``
``fast_ooda_gui_wait_for``                ``axWaitFor``
``fast_ooda_gui_focus_window``            ``focusWindow``
``fast_ooda_gui_record_window_mp4``       ``recordStart`` / ``recordStatus``
                                          / ``recordStop``
========================================  =============================

State and lifetime
------------------
The helper process owns all state: window roots, look records and recording
sessions. One helper is started lazily per Python process and reused, so a
``lookId`` returned by :func:`fast_ooda_gui_look` stays valid for the next
:func:`fast_ooda_gui_act` call in the same process. Call :func:`close` (or let
``atexit`` do it) to stop the helper; after that all look ids are invalid.

Finding the helper
------------------
Resolution order:

1. :func:`configure` with an explicit ``helper_path``
2. the ``AIMUX_GUI_HELPER_PATH`` environment variable
3. ``~/.aimux/helpers/windows-x64-bridge.exe`` (the location the reverse client
   uses; reading it is a path convention, not an aimux API call)
4. ``windows-x64-bridge.exe`` on ``PATH``

Nothing is downloaded: this module never talks to a CDN or a manifest. If the
binary is absent, it raises :class:`LocalGuiError` with the paths it tried.
Start the reverse client once with ``--enable-gui`` to materialise it, or point
``AIMUX_GUI_HELPER_PATH`` at your own copy.

Example
-------
::

    from aimux.local_alignment import (
        fast_ooda_gui_list_windows,
        fast_ooda_gui_look,
        fast_ooda_gui_act,
    )

    roots = fast_ooda_gui_list_windows()["roots"]
    win = next(r for r in roots if "Notepad" in (r.get("title") or ""))
    shot = fast_ooda_gui_look(win["rootRef"], save_image="shot.png")
    print(shot["lookId"], shot["image_path"])

    fast_ooda_gui_act(shot["lookId"], "press", ref="win:look_1:@e3")
"""
from __future__ import annotations

import atexit
import base64
import itertools
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

__all__ = [
    "LocalGuiError",
    "PROTOCOL_VERSION",
    "configure",
    "close",
    "fast_ooda_gui_diagnostics",
    "fast_ooda_gui_list_windows",
    "fast_ooda_gui_look",
    "fast_ooda_gui_look_fast_screenshot",
    "fast_ooda_gui_act",
    "fast_ooda_gui_read_text",
    "fast_ooda_gui_wait_for",
    "fast_ooda_gui_focus_window",
    "fast_ooda_gui_record_window_mp4",
]

# The helper protocol this module speaks. Mirrors the version the shipped
# binaries advertise; a mismatch is a hard error, never a silent downgrade.
PROTOCOL_VERSION = 6

HELPER_PATH_ENV = "AIMUX_GUI_HELPER_PATH"

# Per-command process budget in seconds. These are the helper's own budgets:
# the local path has no broker in the middle, so no extra headroom is added.
REQUEST_TIMEOUTS: dict[str, float] = {
    "diagnostics": 10.0,
    "listRoots": 30.0,
    "listWindows": 30.0,
    "look": 45.0,
    "lookFastScreenshot": 30.0,
    "screenshot": 45.0,
    "act": 75.0,
    "actBatch": 90.0,
    "axReadText": 30.0,
    "uiaReadText": 30.0,
    "axWaitFor": 75.0,
    "uiaWaitFor": 75.0,
    "focusWindow": 20.0,
    "recordStart": 30.0,
    "recordStop": 45.0,
    "recordStatus": 15.0,
}
DEFAULT_REQUEST_TIMEOUT = 45.0

_RECORD_MIN_MS = 1_000
_RECORD_MAX_MS = 600_000
_RECORD_DEFAULT_MS = 10_000


class LocalGuiError(RuntimeError):
    """A helper failure, carrying the protocol's error code.

    ``code`` is the helper's ``error.code`` when the failure came from the
    binary (``stale_look``, ``target_not_found``, ``unsupported_command`` ...),
    or a local code such as ``helper_not_found`` / ``protocol_mismatch``.
    """

    def __init__(self, message: str, code: str = "local_gui_error") -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# helper process
# ---------------------------------------------------------------------------


def _helper_default_paths() -> list[Path]:
    paths = [Path.home() / ".aimux" / "helpers" / "windows-x64-bridge.exe"]
    for name in ("windows-x64-bridge.exe", "aimux-gui-helper.exe"):
        found = shutil.which(name)
        if found:
            paths.append(Path(found))
    return paths


def _resolve_helper_path() -> Path:
    explicit = _configured_path
    if explicit is not None:
        if not explicit.is_file():
            raise LocalGuiError(
                f"configured GUI helper does not exist: {explicit}", code="helper_not_found"
            )
        return explicit

    env_value = os.environ.get(HELPER_PATH_ENV)
    if env_value:
        path = Path(env_value).expanduser()
        if not path.is_file():
            raise LocalGuiError(
                f"{HELPER_PATH_ENV} does not point at a file: {path}", code="helper_not_found"
            )
        return path

    tried = _helper_default_paths()
    for path in tried:
        if path.is_file():
            return path
    locations = ", ".join(str(p) for p in tried) or "(none)"
    raise LocalGuiError(
        "GUI helper binary not found; looked at " + locations + ". "
        "Run `aimux reverse connect --enable-gui` once to install it, "
        f"or set {HELPER_PATH_ENV} to an explicit path.",
        code="helper_not_found",
    )


class _HelperProcess:
    """One helper binary speaking JSON lines on stdin/stdout.

    Requests are serialized through a lock: the protocol is request/response
    with no correlation beyond the ``id``, and one-at-a-time keeps a timed-out
    call from being paired with the next call's answer.
    """

    def __init__(self, binary: Path) -> None:
        self.binary = binary
        self.proc: subprocess.Popen[bytes] | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.started_at = time.time()

    def _spawn(self) -> None:
        try:
            self.proc = subprocess.Popen(
                [str(self.binary)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise LocalGuiError(
                f"could not start GUI helper {self.binary}: {exc}", code="helper_spawn_failed"
            ) from exc
        self.lines = queue.Queue()

        def reader() -> None:
            assert self.proc is not None and self.proc.stdout is not None
            try:
                for raw in self.proc.stdout:
                    self.lines.put(raw.decode("utf-8", "replace").strip())
            except OSError:
                pass
            finally:
                self.lines.put("")  # EOF sentinel

        threading.Thread(target=reader, name="fast-ooda-helper-reader", daemon=True).start()

    def start(self) -> None:
        with self.lock:
            self._spawn()
        probe = self.request("diagnostics", {}, timeout=REQUEST_TIMEOUTS["diagnostics"])
        protocol = int(probe.get("protocolVersion") or 0)
        if protocol != PROTOCOL_VERSION:
            self.stop()
            raise LocalGuiError(
                f"GUI helper protocol mismatch: expected {PROTOCOL_VERSION}, "
                f"got {protocol or 'none'}",
                code="protocol_mismatch",
            )

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def request(self, cmd: str, args: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise LocalGuiError(
                f"helper args must be an object, got {type(args).__name__}", code="invalid_request"
            )
        budget = timeout if timeout is not None else REQUEST_TIMEOUTS.get(cmd, DEFAULT_REQUEST_TIMEOUT)
        payload = json.dumps(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "id": f"l{int(time.time() * 1000)}",
                "cmd": cmd,
                "args": args,
            },
            ensure_ascii=False,
        )
        with self.lock:
            if not self.alive():
                raise LocalGuiError(
                    "GUI helper process is not running", code="helper_not_running"
                )
            assert self.proc is not None and self.proc.stdin is not None
            try:
                # Drop late answers from a previous, already-timed-out request.
                while True:
                    self.lines.get_nowait()
            except queue.Empty:
                pass
            try:
                self.proc.stdin.write(payload.encode("utf-8") + b"\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise LocalGuiError(
                    f"GUI helper pipe write failed: {exc}", code="helper_pipe_error"
                ) from exc
            try:
                line = self.lines.get(timeout=budget)
            except queue.Empty:
                raise LocalGuiError(
                    f"GUI helper timed out after {budget:g}s on {cmd!r}", code="timeout"
                ) from None
        if not line:
            raise LocalGuiError(
                "GUI helper exited before responding", code="helper_exited"
            )
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LocalGuiError(
                f"GUI helper returned malformed JSON: {exc}", code="bad_response"
            ) from exc
        if not isinstance(response, dict):
            raise LocalGuiError("GUI helper returned a non-object response", code="bad_response")
        if response.get("ok") is not True:
            error = response.get("error") if isinstance(response.get("error"), dict) else {}
            raise LocalGuiError(
                str(error.get("message") or "GUI helper command failed"),
                code=str(error.get("code") or "helper_error"),
            )
        result = response.get("result")
        return result if isinstance(result, dict) else {"result": result}

    def stop(self) -> None:
        with self.lock:
            proc = self.proc
            self.proc = None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass


_state_lock = threading.Lock()
_helper: _HelperProcess | None = None
_configured_path: Path | None = None


def _get_helper() -> _HelperProcess:
    global _helper
    with _state_lock:
        if _helper is None or not _helper.alive():
            helper = _HelperProcess(_resolve_helper_path())
            helper.start()
            _helper = helper
        return _helper


def _call(cmd: str, args: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
    return _get_helper().request(cmd, args, timeout=timeout)


def _estimate_tokens(text: str) -> int:
    return -(-len(text.encode("utf-8")) // 4)


def _budget_outline(outline: Any, max_tokens: int) -> dict[str, Any]:
    """Keep the MCP tool's outline budget semantics, applied locally.

    Only used when the caller explicitly passes ``max_output_tokens``; the
    default is no cap, because a local script can consume the whole tree.
    """
    text = json.dumps(outline, ensure_ascii=False)
    estimated = _estimate_tokens(text)
    if max_tokens <= 0 or estimated <= max_tokens:
        return {"estimated_tokens": estimated}
    truncated = text.encode("utf-8")[: max_tokens * 4].decode("utf-8", "ignore")
    return {
        "outline": truncated + "\n...[truncated]...",
        "outline_truncated": True,
        "estimated_tokens": estimated,
    }


def configure(*, helper_path: str | os.PathLike[str] | None = None) -> None:
    """Point the module at a specific helper binary, or reset to discovery.

    Any running helper is stopped first, so look ids from before the call are
    invalid afterwards (they belong to the old process).
    """
    global _configured_path
    close()
    with _state_lock:
        _configured_path = Path(helper_path).expanduser() if helper_path else None


def close() -> None:
    """Stop the lazily started helper, if any. Safe to call repeatedly."""
    global _helper
    with _state_lock:
        helper, _helper = _helper, None
    if helper is not None:
        helper.stop()


atexit.register(close)


# ---------------------------------------------------------------------------
# public commands
# ---------------------------------------------------------------------------


def fast_ooda_gui_diagnostics() -> dict[str, Any]:
    """Report the helper's protocol version, OS, arch, pid and capabilities."""
    return _call("diagnostics", {})


def fast_ooda_gui_list_windows(pid: int | None = None) -> dict[str, Any]:
    """List top-level windows as ``{"roots": [...]}``.

    Returns the helper's full root records (unlike the MCP tool, which trims
    fields and caps the list): a local script can use everything it gets.
    """
    args: dict[str, Any] = {}
    if pid is not None:
        args["pid"] = int(pid)
    return _call("listRoots", args)


def fast_ooda_gui_look(
    root_ref: str | None = None,
    *,
    window_id: int | None = None,
    include_image: bool = True,
    max_dimension: int = 1024,
    max_output_tokens: int | None = None,
    read_text: str | None = None,
    scope_ref: str | None = None,
    base_look_id: str | None = None,
    save_image: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Observe one window: outline tree plus (optionally) a screenshot.

    Parameters mirror ``remote_gui_look``: ``root_ref``, ``include_image``,
    ``max_dimension`` and ``max_output_tokens``. ``window_id``, ``read_text``,
    ``scope_ref``, ``base_look_id`` and ``save_image`` are local-only extras
    that the helper supports but the MCP tool does not expose.

    The result is the helper's raw record: ``lookId``, ``window``, ``outline``,
    ``timings`` and, when ``include_image`` is true, ``image`` with a base64
    payload under ``image.jpegBase64`` (the name is historical; the bytes are
    PNG). When ``save_image`` names a path, the image is also decoded there and
    ``image_path`` is added to the result.

    ``max_output_tokens`` keeps the MCP tool's meaning (a budget for the
    serialized outline) but defaults to ``None`` — a local script normally
    wants the whole tree. Setting it truncates the outline and marks
    ``outline_truncated``.

    Element rectangles are in image pixels, matching the returned screenshot;
    ``fast_ooda_gui_act`` converts image coordinates back to screen pixels.
    """
    if not root_ref and window_id is None:
        raise LocalGuiError(
            "look requires root_ref or window_id", code="invalid_request"
        )
    args: dict[str, Any] = {
        "includeImage": bool(include_image),
        "maxDimension": int(max_dimension),
    }
    if root_ref:
        args["rootRef"] = root_ref
    if window_id is not None:
        args["windowId"] = int(window_id)
    if read_text is not None:
        args["readText"] = read_text
    if scope_ref is not None:
        args["scopeRef"] = scope_ref
    if base_look_id is not None:
        args["baseLookId"] = base_look_id
    result = _call("look", args)
    image = result.get("image") if isinstance(result.get("image"), dict) else None
    if save_image is not None and image and image.get("jpegBase64"):
        path = Path(save_image).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(str(image["jpegBase64"])))
        result["image_path"] = str(path)
    if max_output_tokens is not None:
        result.update(_budget_outline(result.get("outline"), int(max_output_tokens)))
    return result


_FAST_SHOT_SEQUENCE = itertools.count(1)


def _fast_screenshot_target() -> Path:
    """A fresh path under the system temp directory for one fast screenshot."""
    directory = Path(tempfile.gettempdir()) / "aimux-fast-screenshots"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    serial = next(_FAST_SHOT_SEQUENCE)
    return directory / f"shot-{stamp}-{os.getpid()}-{serial:04d}.png"


def _fast_screenshot_fallback(
    root_ref: str | None,
    window_id: int | None,
    max_dimension: int,
    target: Path,
    include_base64: bool,
) -> dict[str, Any]:
    """Serve the same shape from the normal `look` command.

    Used only when the helper binary predates `lookFastScreenshot`; it is
    correct but pays the accessibility-tree cost the fast command avoids.
    """
    shot = fast_ooda_gui_look(
        root_ref,
        window_id=window_id,
        include_image=True,
        max_dimension=max_dimension,
        save_image=target,
    )
    image = shot.get("image") if isinstance(shot.get("image"), dict) else {}
    result: dict[str, Any] = {
        "lookId": shot.get("lookId"),
        "width": image.get("width"),
        "height": image.get("height"),
        "image_path": shot.get("image_path", str(target)),
        "timings": shot.get("timings"),
        "warnings": shot.get("warnings") or [],
    }
    if include_base64 and image.get("jpegBase64"):
        result["image"] = image
    return result


def fast_ooda_gui_look_fast_screenshot(
    root_ref: str | None = None,
    *,
    window_id: int | None = None,
    max_dimension: int = 1024,
    save_path: str | os.PathLike[str] | None = None,
    include_base64: bool = False,
) -> dict[str, Any]:
    """Capture a window screenshot, and do nothing else.

    This is the low-latency sibling of :func:`fast_ooda_gui_look`. It skips the
    accessibility tree entirely, writes the PNG from the helper straight to
    ``save_path`` (no base64 round trip by default), and returns only:

    ``lookId``, ``image_path``, ``width``, ``height``, ``strategy``,
    ``warnings`` and ``timings`` (``captureMs``, ``convertMs``, ``encodeMs``,
    ``writeMs``, ``base64Ms``, ``totalMs``).

    The empty ``outline`` / element records are the point: on a large Chromium
    window the UIA subtree walk costs far more than the pixels themselves.
    The returned ``lookId`` still carries the image geometry, so
    :func:`fast_ooda_gui_act` can target image coordinates from this shot.

    When ``save_path`` is omitted a unique file under the system temp directory
    is used. Pass ``include_base64=True`` only when the caller cannot read the
    local file; it adds the PNG as ``image.jpegBase64`` (the historical field
    name; the bytes are PNG).

    If the helper is older than this command, the call transparently falls back
    to :func:`fast_ooda_gui_look` and marks the result ``fallback: "look"``.
    """
    if not root_ref and window_id is None:
        raise LocalGuiError(
            "fast screenshot requires root_ref or window_id", code="invalid_request"
        )

    target = (
        Path(save_path).expanduser()
        if save_path is not None
        else _fast_screenshot_target()
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    args: dict[str, Any] = {
        "maxDimension": int(max_dimension),
        "savePath": str(target),
        "includeBase64": bool(include_base64),
    }
    if root_ref:
        args["rootRef"] = root_ref
    if window_id is not None:
        args["windowId"] = int(window_id)

    try:
        result = _call("lookFastScreenshot", args)
    except LocalGuiError as exc:
        if exc.code != "unsupported_command":
            raise
        result = _fast_screenshot_fallback(
            root_ref, window_id, max_dimension, target, include_base64
        )
        result["fallback"] = "look"

    image_path = result.pop("imagePath", None) or str(target)
    result["image_path"] = str(image_path)
    return result


def fast_ooda_gui_act(
    look_id: str,
    action: str,
    *,
    ref: str | None = None,
    x: float | None = None,
    y: float | None = None,
    policy: str = "ax_only",
    text: str | None = None,
    keys: list[str] | None = None,
    scroll_x: float | None = None,
    scroll_y: float | None = None,
    keep_pressdown_ms: int | None = None,
    path: list[dict[str, float]] | None = None,
    preserve_focus: bool | None = None,
    defer_root_delta: bool | None = None,
) -> dict[str, Any]:
    """Act on an element or point observed by a previous ``look``.

    ``ref`` drives UIA patterns (semantic delivery, no synthetic input).
    ``x``/``y`` are image-pixel coordinates and require ``policy="foreground"``,
    as do ``typeText``, ``keypress``, ``drag`` and ``moveMouse``.

    ``keys`` is a top-level argument here, matching the MCP tool; it is packed
    into ``params.keys`` for the helper, which is where the protocol expects it.
    """
    if not isinstance(look_id, str) or not look_id:
        raise LocalGuiError("look_id is required", code="invalid_request")
    if not isinstance(action, str) or not action:
        raise LocalGuiError("action is required", code="invalid_request")
    if policy not in ("ax_only", "default", "foreground"):
        raise LocalGuiError(
            "policy must be ax_only, default, or foreground", code="invalid_request"
        )
    if isinstance(ref, str) and ref:
        target: dict[str, Any] = {"ref": ref}
    elif x is not None and y is not None:
        target = {"x": float(x), "y": float(y)}
    else:
        raise LocalGuiError(
            "target needs ref (preferred) or x/y image coordinates", code="invalid_request"
        )

    params: dict[str, Any] = {}
    if text is not None:
        params["text"] = text
    if keys is not None:
        params["keys"] = keys
    if scroll_x is not None:
        params["scrollX"] = scroll_x
    if scroll_y is not None:
        params["scrollY"] = scroll_y
    if keep_pressdown_ms is not None:
        params["keepPressdownMs"] = keep_pressdown_ms
    if path is not None:
        params["path"] = path
    if preserve_focus is not None:
        params["preserveFocus"] = bool(preserve_focus)

    args: dict[str, Any] = {
        "lookId": look_id,
        "action": action,
        "policy": policy,
        "target": target,
        "params": params,
    }
    if defer_root_delta is not None:
        args["deferRootDelta"] = bool(defer_root_delta)
    return _call("act", args)


def fast_ooda_gui_read_text(
    look_id: str,
    ref: str,
    *,
    offset: int = 0,
    limit: int = 4096,
) -> dict[str, Any]:
    """Read one element's live text (TextPattern -> ValuePattern -> name).

    Parameters and defaults mirror ``remote_gui_read_text``. Cheaper than a
    fresh look when only a value changed. Secure fields are refused by the
    helper with ``secure_text_unreadable``.
    """
    if not isinstance(look_id, str) or not look_id:
        raise LocalGuiError("look_id is required", code="invalid_request")
    if not isinstance(ref, str) or not ref:
        raise LocalGuiError("ref is required", code="invalid_request")
    args: dict[str, Any] = {
        "lookId": look_id,
        "elementRef": ref,
        "offset": int(offset),
        "limit": int(limit),
    }
    return _call("axReadText", args)


def fast_ooda_gui_wait_for(
    *,
    text: str | None = None,
    role: str | None = None,
    value: str | None = None,
    gone: bool = False,
    timeout_ms: int = 10_000,
    scope_exact: bool = False,
    look_id: str | None = None,
    scope_ref: str | None = None,
    window_id: int | None = None,
    root_ref: str | None = None,
    pid: int | None = None,
) -> dict[str, Any]:
    """Poll the live accessibility tree until a match appears (or disappears).

    Parameters and defaults mirror ``remote_gui_wait_for``. The helper does
    the polling (``timeoutMs`` is clamped to 100..60000); the process budget is
    that timeout plus a small margin for the round trip.
    """
    if not any((text, role, value)):
        raise LocalGuiError(
            "wait_for requires at least one of text, role, or value", code="invalid_request"
        )
    args: dict[str, Any] = {
        "gone": bool(gone),
        "timeoutMs": int(timeout_ms),
        "scopeExact": bool(scope_exact),
    }
    optional = (
        ("text", text),
        ("role", role),
        ("value", value),
        ("lookId", look_id),
        ("scopeRef", scope_ref),
        ("windowId", window_id),
        ("rootRef", root_ref),
        ("pid", pid),
    )
    for key, item in optional:
        if item is not None:
            args[key] = item
    return _call("axWaitFor", args, timeout=int(timeout_ms) / 1000.0 + 15.0)


def fast_ooda_gui_focus_window(root_ref: str) -> dict[str, Any]:
    """Bring one window to the foreground."""
    if not isinstance(root_ref, str) or not root_ref:
        raise LocalGuiError("root_ref is required", code="invalid_request")
    return _call("focusWindow", {"rootRef": root_ref})


def fast_ooda_gui_record_window_mp4(
    root_ref: str | None = None,
    *,
    window_id: int | None = None,
    duration_ms: int = _RECORD_DEFAULT_MS,
    record_fps: int | None = None,
    fps: int | None = None,
    max_dimension: int = 0,
    audio: str = "system",
    local_path: str | os.PathLike[str] | None = None,
    overwrite: bool = False,
    poll_seconds: float = 2.0,
) -> dict[str, Any]:
    """Record one window to MP4 and return the file that already exists locally.

    Parameters mirror ``remote_gui_record_window_mp4`` (``record_fps`` wins
    over the older ``fps`` alias). Unlike the MCP tool there is no download
    step: the script and the helper share a filesystem, so ``video_path`` is
    either the helper's own output path or the ``local_path`` you asked it to
    be copied to.

    ``record_fps`` is 1..30, ``duration_ms`` 1000..600000, ``max_dimension`` 0
    means "the window's own size". Recording blocks for the requested duration
    (or until the window closes), mirroring the tool.
    """
    if not root_ref and window_id is None:
        raise LocalGuiError(
            "record requires root_ref or window_id", code="invalid_request"
        )
    if audio not in ("system", "mic", "none"):
        raise LocalGuiError("audio must be one of: system, mic, none", code="invalid_request")
    duration = max(_RECORD_MIN_MS, min(_RECORD_MAX_MS, int(duration_ms)))
    if int(max_dimension) < 0:
        raise LocalGuiError("max_dimension cannot be negative", code="invalid_request")
    # MCP precedence: record_fps wins, fps is the older alias, 10 is the default.
    effective_fps = record_fps if record_fps is not None else (fps if fps is not None else 10)

    start_args: dict[str, Any] = {
        "fps": int(effective_fps),
        "maxDurationMs": duration,
        "maxDimension": int(max_dimension),
        "audio": audio,
    }
    if root_ref:
        start_args["rootRef"] = root_ref
    if window_id is not None:
        start_args["windowId"] = int(window_id)

    start = _call("recordStart", start_args)
    recording_id = str(start.get("recordingId") or "")
    if not recording_id:
        raise LocalGuiError(
            f"the recording helper did not return a recordingId: {start}",
            code="bad_response",
        )

    deadline = time.monotonic() + duration / 1000.0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.05, poll_seconds), remaining))
        try:
            status = _call("recordStatus", {"recordingId": recording_id})
        except LocalGuiError:
            break  # recordStop reports the real outcome
        if not status.get("active"):
            break

    stop = _call("recordStop", {"recordingId": recording_id})
    error = str(stop.get("error") or "")
    remote_path = str(stop.get("path") or start.get("path") or "")
    if error and not stop.get("bytes"):
        raise LocalGuiError(f"recording failed: {error}", code="record_failed")
    if not remote_path:
        raise LocalGuiError(
            f"the recording helper did not report a file path: {stop}", code="bad_response"
        )

    source = Path(remote_path)
    if local_path is None:
        target = source
    else:
        target = Path(local_path).expanduser().resolve()
        if target.is_dir():
            raise LocalGuiError(f"local_path resolves to a directory: {target}", code="invalid_request")
        if target.exists() and not overwrite:
            raise LocalGuiError(
                f"local file already exists (pass overwrite=True to replace): {target}",
                code="file_exists",
            )
        if target != source.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    out: dict[str, Any] = {
        "video_path": str(target),
        "size": target.stat().st_size if target.is_file() else 0,
        "recording_id": recording_id,
        "remote_path": remote_path,
        "duration_ms": stop.get("durationMs"),
        "width": stop.get("width") or start.get("width"),
        "height": stop.get("height") or start.get("height"),
        "fps": stop.get("fps") or start.get("fps"),
        "max_dimension": start.get("maxDimension"),
        "audio": stop.get("audio") or start.get("audio"),
        "has_audio": bool(stop.get("hasAudio", start.get("hasAudio"))),
        "video_frames": stop.get("videoFrames"),
        "audio_frames": stop.get("audioFrames"),
    }
    warning = stop.get("audioWarning") or start.get("audioWarning")
    if warning:
        out["audio_warning"] = warning
    if error:
        out["error"] = error
    return out
