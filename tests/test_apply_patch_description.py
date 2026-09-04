"""Tests that the remote_apply_patch MCP tool description carries the hunk spec
the model needs (codex-style example + the two hard rules that prevent
'patch context not found'). Non-GPT models (e.g. GLM) rely on this text
because they lack codex's training-time familiarity with apply_patch.
"""
from __future__ import annotations

from aimux import mcp_server


def _desc() -> str:
    t = next(x for x in mcp_server.TOOLS if x["name"] == "remote_apply_patch")
    return t["description"]


def test_description_has_codex_example_with_hunk():
    d = _desc()
    # The example must show the hunk shape, not just say "codex-style".
    assert "*** Begin Patch" in d
    assert "*** Update File:" in d
    assert "@@" in d  # hunk delimiter, with context on the same line
    assert "-" in d and "+" in d  # remove/insert line prefixes


def test_description_requires_one_edit_site_per_hunk():
    d = _desc()
    assert "one edit site per" in d
    assert "hunk" in d


def test_description_requires_verbatim_context():
    d = _desc()
    assert "verbatim" in d or "byte-for-byte" in d
    assert "context" in d


def test_description_warns_about_context_not_found():
    d = _desc()
    # The exact error the model will hit if it ignores the spec.
    assert "patch context not found" in d


def test_input_field_description_restates_spec():
    t = next(x for x in mcp_server.TOOLS if x["name"] == "remote_apply_patch")
    inp = t["inputSchema"]["properties"]["input"]["description"]
    assert "verbatim" in inp
    assert "@@" in inp or "hunk" in inp
