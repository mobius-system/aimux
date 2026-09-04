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


def test_patch_relative_path_uses_workdir(mcp, tmp_path):
    workdir = tmp_path
    res = mcp.call(
        "remote_apply_patch",
        workdir=str(workdir),
        input=_patch("*** Add File: rel/sub/child.txt\n+relative ok\n"),
    )
    assert res["applied"] == [f"added: {workdir / 'rel' / 'sub' / 'child.txt'}"], res
    assert (workdir / "rel" / "sub" / "child.txt").read_text() == "relative ok"
