"""remote_apply_patch — real bridge file ops on the container FS. 6 tests."""
from __future__ import annotations

import pytest


def _patch(ops: str) -> str:
    # strip() avoids a stray empty line before *** End Patch (an empty add-line
    # would otherwise smuggle a trailing newline into Add File content).
    return f"*** Begin Patch\n{ops.strip()}\n*** End Patch\n"


def test_patch_add_file(mcp, tmp_path):
    p = tmp_path / "new.txt"
    res = mcp.call("remote_apply_patch", input=_patch(
        f"*** Add File: {p}\n+hello patch\n"
    ))
    assert res["applied"] == [f"added: {p}"], res
    assert p.read_text() == "hello patch"


def test_patch_update_file_with_context(mcp, tmp_path):
    p = tmp_path / "src.txt"
    p.write_text("alpha\nmiddle\nomega\n")
    res = mcp.call("remote_apply_patch", input=_patch(
        f"*** Update File: {p}\n@@\n-alpha\n+ALPHA\n"
    ))
    assert res["applied"] == [f"updated: {p}"], res
    assert p.read_text() == "ALPHA\nmiddle\nomega\n"


def test_patch_delete_file(mcp, tmp_path):
    p = tmp_path / "victim.txt"
    p.write_text("bye")
    res = mcp.call("remote_apply_patch", input=_patch(
        f"*** Delete File: {p}\n"
    ))
    assert res["applied"] == [f"deleted: {p}"], res
    assert not p.exists()


def test_patch_update_missing_context_raises(mcp, tmp_path):
    p = tmp_path / "ctx.txt"
    p.write_text("real-context\n")
    with pytest.raises(RuntimeError, match="patch context not found"):
        mcp.call("remote_apply_patch", input=_patch(
            f"*** Update File: {p}\n@@\n-this-context-is-not-in-the-file\n+replaced\n"
        ))
    # File must be untouched on failure.
    assert p.read_text() == "real-context\n"


def test_patch_multi_hunk_one_file(mcp, tmp_path):
    p = tmp_path / "multi.txt"
    p.write_text("one\ntwo\nthree\nfour\nfive\n")
    res = mcp.call("remote_apply_patch", input=_patch(
        f"*** Update File: {p}\n"
        f"@@\n-one\n+ONE\n"
        f"@@\n-five\n+FIVE\n"
    ))
    assert res["applied"] == [f"updated: {p}"], res
    assert p.read_text() == "ONE\ntwo\nthree\nfour\nFIVE\n"


def test_patch_relative_path_rejected(mcp, tmp_path):
    with pytest.raises(RuntimeError, match="must be absolute"):
        mcp.call(
            "remote_apply_patch",
            workdir=str(tmp_path),  # even with workdir supplied, relative paths now refuse
            input=_patch("*** Add File: rel/sub/child.txt\n+relative ok\n"),
        )


def test_read_pages_and_reports_metadata(mcp, tmp_path):
    """The CLI `aimux read` command against the real bridge remote."""
    import subprocess, json as _json

    p = tmp_path / "readme.txt"
    p.write_text("\n".join(f"line-{i}" for i in range(1, 31)) + "\n")
    out = subprocess.run(
        ["aimux", "read", "localbox", "--path", str(p), "--offset", "5", "--limit", "2", "--json"],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    obj = _json.loads(out.stdout)
    assert obj["total_lines"] == 30
    assert obj["offset"] == 5 and obj["limit"] == 2
    assert obj["truncated"] is True
    assert obj["content"] == "5→line-5\n6→line-6"


def test_cli_apply_patch_remote_flag(mcp, tmp_path):
    """`aimux apply-patch --remote localbox` full round-trip over the bridge."""
    import subprocess

    p = tmp_path / "remote-target.txt"
    p.write_text("before\n")
    patch = f"*** Begin Patch\n*** Update File: {p}\n@@\n-before\n+after\n*** End Patch\n"
    out = subprocess.run(
        ["aimux", "apply-patch", "--remote", "localbox"],
        input=patch, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "updated:" in out.stdout
    assert p.read_text() == "after\n"
