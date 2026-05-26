"""Output helpers: text-vs-JSON, table-vs-quiet."""
from __future__ import annotations

import json
import sys
from typing import Any, Iterable, Sequence


def print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, sort_keys=False))


def print_error_json(code: int, kind: str, message: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"error": {"code": code, "kind": kind, "message": message}}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))


def emit_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    rows = list(rows)
    cols = [str(h) for h in headers]
    widths = [len(c) for c in cols]
    str_rows: list[list[str]] = []
    for row in rows:
        srow = ["" if v is None else str(v) for v in row]
        str_rows.append(srow)
        for i, cell in enumerate(srow):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    line = "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols))
    print(line)
    for srow in str_rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(srow)))


def err(msg: str) -> None:
    print(msg, file=sys.stderr)
