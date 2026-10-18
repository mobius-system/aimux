#!/usr/bin/env python3
"""Live desktop window list in the terminal — a tiny manual test program.

Run on the machine whose desktop you want to watch:

    python window_board.py              # redraw every second, Ctrl+C to stop
    python window_board.py --once       # print one frame and exit
    python window_board.py -n 0.5       # different refresh interval

It asks the local GUI helper directly (via aimux.local_alignment): no MCP, no
bridge, no HTTP. Standard library only; if the helper is not in the default
place, set AIMUX_GUI_HELPER_PATH.
"""
import sys
import time

try:
    from aimux.local_alignment import fast_ooda_gui_list_windows
except ImportError:  # running next to a copied local_alignment.py
    from local_alignment import fast_ooda_gui_list_windows

CLEAR = "\033[2J\033[H"  # works in Windows Terminal and every Unix terminal


def frame():
    roots = fast_ooda_gui_list_windows().get("roots") or []
    lines = [
        f"{time.strftime('%H:%M:%S')}  {len(roots)} windows",
        "",
        f"  {'PROCESS':<24} {'PID':>6}  {'REF':<5} TITLE",
    ]
    for root in roots:
        mark = "*" if root.get("isFocused") else " "
        process = (root.get("processName") or "")[:24]
        title = root.get("title") or "(untitled)"
        if root.get("isMinimized"):
            title += "  [minimized]"
        lines.append(
            f"{mark} {process:<24} {str(root.get('pid')):>6}  "
            f"{root.get('rootRef', ''):<5} {title}"
        )
    return "\n".join(lines)


def main(argv):
    # Window titles contain bullets and emoji; a GBK console would otherwise
    # raise UnicodeEncodeError mid-frame. Keep the encoding, soften the rest.
    try:
        sys.stdout.reconfigure(errors="backslashreplace")
    except Exception:
        pass
    once = "--once" in argv
    interval = 1.0
    if "-n" in argv:
        interval = float(argv[argv.index("-n") + 1])
    if not once:
        sys.stdout.write("\033[?25l")  # hide the cursor while redrawing
    try:
        while True:
            try:
                text = frame()
            except Exception as exc:  # keep the loop alive, show the reason
                text = f"{time.strftime('%H:%M:%S')}  error: {exc}"
            if once:
                print(text)
                return
            sys.stdout.write(CLEAR + text + "\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        if not once:
            sys.stdout.write("\033[?25h\n")  # show the cursor again


if __name__ == "__main__":
    main(sys.argv[1:])
