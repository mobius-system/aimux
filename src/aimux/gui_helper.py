"""GUI accessibility helper manager for reverse clients.

Owns the lifecycle of the native computer-use helper (a single Rust binary
speaking JSON lines over stdin/stdout). Responsibilities:

  install()  fetch the helper binary from the CDN manifest, verify sha256,
             and land it atomically under ~/.aimux/helpers/
  GuiHelper  spawn the binary, forward one request -> one response line,
             and surface probe/diagnostics state
  start()    one-shot entry used by bridge_client: install + spawn + probe,
             never raising — the caller gets a status dict either way

Failure semantics: a broken GUI must never take the reverse connection down.
Every failure path returns {"available": False, "error": ...} instead.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

# The helper speaks protocol 5 (pi-computer-use windows-bridge v0.5.1 plus the
# window-recording commands).
EXPECTED_PROTOCOL_VERSION = 6
DEFAULT_MANIFEST_URL = "https://serve.nutshellai.cn/publish/auto/aimux-helpers/manifest.json"

# Per-command budget for one helper round trip, in seconds. waitFor gets the
# longest because it polls the live UIA subtree for up to 60s by itself.
REQUEST_TIMEOUTS: dict[str, float] = {
    "diagnostics": 10.0,
    "listRoots": 30.0,
    "listWindows": 30.0,
    "look": 45.0,
    "screenshot": 45.0,
    "act": 75.0,
    "actBatch": 90.0,
    "uiaReadText": 30.0,
    "axReadText": 30.0,
    "uiaWaitFor": 75.0,
    "axWaitFor": 75.0,
    "focusWindow": 20.0,
    "openBrowserLocation": 30.0,
    # recordStart answers once the first frame is encoded, but it opens the
    # capture device first; recordStop waits for the encoder to finalize the
    # MP4, which is why it gets the longest budget of the three.
    "recordStart": 30.0,
    "recordStop": 45.0,
    "recordStatus": 15.0,
}
DEFAULT_REQUEST_TIMEOUT = 45.0

_INSTALL_LOCK = threading.Lock()


class GuiHelperError(RuntimeError):
    """Raised for helper-side failures that map onto a bridge error."""


def is_elevated() -> bool:
    """True when this process runs with an elevated (administrator) token.

    Windows only concept; elsewhere the question is meaningless and we report
    False. GUI input into elevated windows (games with kernel anti-cheat,
    admin apps) is blocked by UIPI unless the helper itself is elevated.
    """
    if platform.system() != "Windows":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _platform_key() -> str | None:
    machine = (platform.machine() or "").lower()
    if platform.system() == "Windows":
        # ARM64 Windows is not shipped yet; the x64 binary runs under
        # emulation but that is not something we want to promise.
        return "windows-x64" if machine in ("amd64", "x86_64", "x64") else None
    return None


def helper_dir() -> Path:
    return Path.home() / ".aimux" / "helpers"


def _download(url: str, dest: Path, *, timeout: float = 120.0, expected_size: int | None = None) -> None:
    tmp = dest.with_name(dest.name + f".part-{os.getpid()}")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(tmp, "wb") as out:
            shutil.copyfileobj(resp, out)
        if expected_size is not None and tmp.stat().st_size != expected_size:
            raise GuiHelperError(
                f"helper download size mismatch: expected {expected_size}, got {tmp.stat().st_size}"
            )
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_manifest_entry(manifest_url: str, platform_key: str) -> dict[str, Any]:
    with urllib.request.urlopen(manifest_url, timeout=15.0) as resp:
        manifest = json.loads(resp.read().decode("utf-8"))
    entry = (manifest.get("helpers") or {}).get(platform_key)
    if not isinstance(entry, dict) or not entry.get("url"):
        raise GuiHelperError(f"helper manifest has no entry for {platform_key!r}")
    return entry


def install() -> Path:
    """Make sure the helper binary for this platform exists and matches the
    manifest. Returns the binary path; raises GuiHelperError on failure.

    Env overrides (dev/debug, no CDN round trip):
      AIMUX_GUI_HELPER_PATH  use this binary directly (skips download AND the
                             platform gate, so any host can drive a mock helper)
      AIMUX_GUI_HELPER_URL   download this URL instead of the manifest's
      AIMUX_GUI_MANIFEST_URL use this manifest
    """
    direct = os.environ.get("AIMUX_GUI_HELPER_PATH")
    if direct:
        path = Path(direct)
        if not path.is_file():
            raise GuiHelperError(f"AIMUX_GUI_HELPER_PATH does not exist: {direct}")
        return path

    platform_key = _platform_key()
    if platform_key is None:
        raise GuiHelperError(
            "GUI helpers are only available for Windows x64 in this release "
            f"(this machine: {platform.system()} {platform.machine()})"
        )

    dest = helper_dir() / f"{platform_key}-bridge{'.exe' if platform.system() == 'Windows' else ''}"
    meta_path = dest.with_name(dest.name + ".meta.json")

    manifest_url = os.environ.get("AIMUX_GUI_MANIFEST_URL") or DEFAULT_MANIFEST_URL
    entry = _resolve_manifest_entry(manifest_url, platform_key)
    url = os.environ.get("AIMUX_GUI_HELPER_URL") or str(entry["url"])
    sha256 = str(entry.get("sha256") or "").lower()
    size = int(entry.get("size") or 0) or None
    if not sha256:
        raise GuiHelperError("helper manifest entry is missing sha256")

    with _INSTALL_LOCK:
        if dest.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            if meta.get("sha256") == sha256:
                return dest
            # Version drift: keep the old binary until the new one landed.
        dest.parent.mkdir(parents=True, exist_ok=True)
        _download(url, dest, expected_size=size)
        actual = _sha256_file(dest)
        if actual != sha256:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise GuiHelperError(f"helper sha256 mismatch: expected {sha256}, got {actual}")
        meta_path.write_text(
            json.dumps({"sha256": sha256, "helperVersion": entry.get("helperVersion", "")}),
            encoding="utf-8",
        )
        return dest


class GuiHelper:
    """One running helper process. Requests are serialized through a lock —
    the binary is internally concurrent, but one-at-a-time keeps bridge error
    reporting unambiguous."""

    def __init__(self, binary: Path):
        self.binary = binary
        self.proc: subprocess.Popen[bytes] | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self.lock = threading.Lock()
        self.started_at = time.time()

    def _spawn(self) -> None:
        # Binary pipes + explicit UTF-8: the helper emits UTF-8 JSON, and a
        # text-mode pipe on Windows would decode as the OEM codepage and
        # corrupt any non-ASCII window title badly enough to break parsing.
        self.proc = subprocess.Popen(
            [str(self.binary)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
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

        threading.Thread(target=reader, name="aimux-gui-helper-reader", daemon=True).start()

    def start(self) -> None:
        with self.lock:
            self._spawn()
        probe = self.request("diagnostics", {}, timeout=REQUEST_TIMEOUTS["diagnostics"])
        result = probe.get("result") or {}
        protocol = int(result.get("protocolVersion") or 0)
        if not probe.get("ok") or protocol != EXPECTED_PROTOCOL_VERSION:
            self.stop()
            raise GuiHelperError(
                f"helper protocol mismatch: expected {EXPECTED_PROTOCOL_VERSION}, got {protocol or 'none'}"
            )

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def request(self, cmd: str, args: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if not isinstance(args, dict):
            raise GuiHelperError(f"helper args must be an object, got {type(args).__name__}")
        budget = timeout if timeout is not None else REQUEST_TIMEOUTS.get(cmd, DEFAULT_REQUEST_TIMEOUT)
        payload = json.dumps(
            {"protocolVersion": EXPECTED_PROTOCOL_VERSION, "id": f"m{int(time.time() * 1000)}", "cmd": cmd, "args": args}
        )
        with self.lock:
            if not self.alive():
                raise GuiHelperError("GUI helper process is not running")
            assert self.proc is not None and self.proc.stdin is not None
            try:
                # Drain any stale lines (e.g. late responses after a timeout).
                while True:
                    self.lines.get_nowait()
            except queue.Empty:
                pass
            try:
                self.proc.stdin.write(payload.encode("utf-8") + b"\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise GuiHelperError(f"GUI helper pipe write failed: {exc}") from exc
            try:
                line = self.lines.get(timeout=budget)
            except queue.Empty:
                raise GuiHelperError(f"GUI helper timed out after {budget:g}s on {cmd!r}") from None
        if not line:
            raise GuiHelperError("GUI helper exited before responding")
        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GuiHelperError(f"GUI helper returned malformed JSON: {exc}") from exc
        if not isinstance(response, dict):
            raise GuiHelperError("GUI helper returned a non-object response")
        return response

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


def probe_status(helper: GuiHelper) -> dict[str, Any]:
    """Run diagnostics and shape the registration payload's gui field."""
    response = helper.request("diagnostics", {}, timeout=REQUEST_TIMEOUTS["diagnostics"])
    if not response.get("ok"):
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        raise GuiHelperError(str(error.get("message") or "helper diagnostics failed"))
    result = response.get("result") or {}
    status = {
        "available": True,
        "protocolVersion": result.get("protocolVersion"),
        "os": result.get("os"),
        "arch": result.get("arch"),
        "pid": result.get("pid"),
    }
    if platform.system() == "Windows":
        # Surface the elevation state so remote ls / the web device picker can
        # tell "GUI works" from "GUI works and can drive elevated windows".
        status["elevated"] = is_elevated()
    return status


def start() -> tuple[GuiHelper | None, dict[str, Any]]:
    """Install + spawn + probe. Returns (helper, status); helper is None on
    any failure, with the reason in status['error']. Never raises."""
    try:
        binary = install()
    except GuiHelperError as exc:
        return None, {"available": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - network shapes we cannot predict
        return None, {"available": False, "error": f"helper install failed: {exc}"}
    helper = GuiHelper(binary)
    try:
        helper.start()
        status = probe_status(helper)
    except Exception as exc:  # noqa: BLE001
        helper.stop()
        return None, {"available": False, "error": f"helper startup failed: {exc}"}
    return helper, status
