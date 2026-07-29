"""Win32 console helpers for visible bridge sessions."""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes

from .bridge import BridgeError
from .bridge_client import _parse_repeat


if sys.platform == "win32":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # CreateFileW returns a fresh handle to the *currently attached* console's
    # input/output buffer. This is what we must use after AttachConsole —
    # GetStdHandle returns the process's startup-time standard handle, which is
    # stale (or INVALID_HANDLE_VALUE) when the reverse client has no console of
    # its own (e.g. spawned as an Electron child / background service). That
    # staleness was the root cause of "failed to write console input (Win32
    # error 6)".
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
else:  # pragma: no cover - imported only to keep non-Windows syntax checks happy.
    kernel32 = None


# AttachConsole + FreeConsole are process-wide: a process is attached to at
# most one console at a time. Guard the attach → operate → free sequence so that
# concurrent calls (send_keys to one session racing capture of another) cannot
# detach each other's console mid-operation. Today commands are dispatched from
# a single SSE reader thread, but the Win32 console API's process-global nature
# makes this lock a necessary safety net.
_console_lock = threading.Lock()


STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
ATTACH_PARENT_PROCESS = -1
KEY_EVENT = 0x0001

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
# INVALID_HANDLE_VALUE is (HANDLE)-1; represent it as the unsigned pointer-sized
# int that CreateFileW actually returns via a c_void_p restype.
INVALID_HANDLE_VALUE = (1 << (8 * ctypes.sizeof(wintypes.HANDLE))) - 1

VK = {
    "Enter": 0x0D,
    "C-m": 0x0D,
    "Tab": 0x09,
    "Backspace": 0x08,
    "BSpace": 0x08,
    "Escape": 0x1B,
    "Esc": 0x1B,
    "Space": 0x20,
    "Up": 0x26,
    "Down": 0x28,
    "Left": 0x25,
    "Right": 0x27,
    "Home": 0x24,
    "End": 0x23,
    "Delete": 0x2E,
    "DC": 0x2E,
}

SPECIAL_CHAR = {
    "Enter": "\r",
    "C-m": "\r",
    "Tab": "\t",
    "Backspace": "\b",
    "BSpace": "\b",
    "Escape": "\x1b",
    "Esc": "\x1b",
    "Space": " ",
    "C-c": "\x03",
    "C-d": "\x04",
    "C-l": "\x0c",
}


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", wintypes.SHORT),
        ("Top", wintypes.SHORT),
        ("Right", wintypes.SHORT),
        ("Bottom", wintypes.SHORT),
    ]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD),
        ("dwCursorPosition", COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", SMALL_RECT),
        ("dwMaximumWindowSize", COORD),
    ]


class CHAR_UNION(ctypes.Union):
    _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", CHAR_UNION),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class EVENT_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", EVENT_UNION)]


def _check_windows() -> None:
    if sys.platform != "win32" or kernel32 is None:
        raise BridgeError("Win32 console helpers are only available on Windows")


def _raise_last(message: str) -> None:
    code = ctypes.get_last_error()
    raise BridgeError(f"{message} (Win32 error {code})")


def _attach(pid: int) -> None:
    _check_windows()
    _console_lock.acquire()
    # ERROR_INVALID_HANDLE (6) can surface right after CREATE_NEW_CONSOLE when
    # the child's console isn't ready yet, or during rapid session churn — retry
    # a short window before giving up.
    last_code = 0
    for _ in range(20):
        kernel32.FreeConsole()
        if kernel32.AttachConsole(wintypes.DWORD(pid)):
            return
        last_code = ctypes.get_last_error()
        if last_code != 6:
            break
        time.sleep(0.1)
    _console_lock.release()
    raise BridgeError(f"failed to attach to console for pid {pid} (Win32 error {last_code})")


def _free() -> None:
    try:
        if kernel32 is not None:
            kernel32.FreeConsole()
    finally:
        _console_lock.release()


def _is_invalid_handle(handle) -> bool:
    return handle is None or handle == INVALID_HANDLE_VALUE


def _open_conin():
    """Open a writable handle to the currently attached console's input buffer.

    Must be called after _attach(). Unlike GetStdHandle(STD_INPUT_HANDLE),
    CreateFileW("CONIN$") resolves to the live console that AttachConsole just
    bound us to, so it works even when the process has no inherited console.
    """
    handle = kernel32.CreateFileW(
        "CONIN$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if _is_invalid_handle(handle):
        _raise_last("failed to open console input (CONIN$)")
    return handle


def _open_conout():
    """Open a readable handle to the currently attached console's output buffer."""
    handle = kernel32.CreateFileW(
        "CONOUT$",
        GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if _is_invalid_handle(handle):
        _raise_last("failed to open console output (CONOUT$)")
    return handle


def _close(handle) -> None:
    """Close a handle returned by _open_conin/_open_conout. Safe on None/invalid."""
    if not _is_invalid_handle(handle):
        kernel32.CloseHandle(handle)


def capture(pid: int, lines: int) -> str:
    if lines <= 0:
        raise BridgeError("--lines must be a positive integer")
    _attach(pid)
    handle = None
    try:
        handle = _open_conout()
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            _raise_last("failed to read console buffer info")
        width = int(info.dwSize.X)
        cursor_y = int(info.dwCursorPosition.Y)
        start_y = max(0, cursor_y - lines + 1)
        rows: list[str] = []
        for y in range(start_y, cursor_y + 1):
            buf = ctypes.create_unicode_buffer(width)
            read = wintypes.DWORD(0)
            coord = COORD(0, y)
            ok = kernel32.ReadConsoleOutputCharacterW(
                handle,
                buf,
                wintypes.DWORD(width),
                coord,
                ctypes.byref(read),
            )
            if not ok:
                _raise_last("failed to read console row")
            rows.append(buf.value[: read.value].rstrip())
        while rows and not rows[-1].strip():
            rows.pop()
        content = "\n".join(rows[-lines:])
        return content + "\n" if content else ""
    finally:
        _close(handle)
        _free()


def configure_buffer(pid: int, *, height: int) -> None:
    _attach(pid)
    handle = None
    try:
        handle = _open_conout()
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return
        width = max(1, int(info.dwSize.X))
        target = COORD(width, max(int(info.dwSize.Y), height))
        kernel32.SetConsoleScreenBufferSize(handle, target)
    finally:
        _close(handle)
        _free()


def send_keys(pid: int, keys: list[str], flags: list[str]) -> None:
    repeat, literal = _parse_repeat(flags)
    records: list[INPUT_RECORD] = []
    for _ in range(repeat):
        for key in keys:
            if not literal and key in SPECIAL_CHAR:
                for ch in SPECIAL_CHAR[key]:
                    records.extend(_char_records(ch))
            elif not literal and key in VK:
                records.extend(_key_records(VK[key], ""))
            else:
                for ch in key:
                    records.extend(_char_records(ch))
    if not records:
        return
    _attach(pid)
    handle = None
    try:
        handle = _open_conin()
        arr_type = INPUT_RECORD * len(records)
        arr = arr_type(*records)
        written = wintypes.DWORD(0)
        ok = kernel32.WriteConsoleInputW(handle, arr, wintypes.DWORD(len(records)), ctypes.byref(written))
        if not ok or written.value != len(records):
            _raise_last("failed to write console input")
    finally:
        _close(handle)
        _free()


def _char_records(ch: str) -> list[INPUT_RECORD]:
    vk = VK.get("Enter") if ch == "\r" else 0
    return _key_pair(vk or 0, ch)


def _key_records(vk: int, ch: str) -> list[INPUT_RECORD]:
    return _key_pair(vk, ch)


def _key_pair(vk: int, ch: str) -> list[INPUT_RECORD]:
    return [_record(True, vk, ch), _record(False, vk, ch)]


def _record(down: bool, vk: int, ch: str) -> INPUT_RECORD:
    rec = INPUT_RECORD()
    rec.EventType = KEY_EVENT
    rec.Event.KeyEvent.bKeyDown = bool(down)
    rec.Event.KeyEvent.wRepeatCount = 1
    rec.Event.KeyEvent.wVirtualKeyCode = vk
    rec.Event.KeyEvent.wVirtualScanCode = 0
    rec.Event.KeyEvent.uChar.UnicodeChar = ch if ch else "\x00"
    rec.Event.KeyEvent.dwControlKeyState = 0
    return rec
