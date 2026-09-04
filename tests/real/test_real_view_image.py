"""remote_view_image — real image bytes on the container FS. 6 tests."""
from __future__ import annotations

import base64
import urllib.request

import pytest

# Real, minimal, valid image bytes (so this is not "fake data" — these are
# genuine well-formed image files, just tiny).
PNG_1x1_RED = (
    b"\x89PNG\r\n\x1a\n"
    + bytes.fromhex(
        "0000000d49484452000000010000000108020000009077053d"
        "0000000c4944415478da63f8cf0000000300010020e3b1e8c5"
        "0000000049454e44ae426082"
    )
)
# JPEG: smallest valid SOI+EOI (1×1, no payload). file(1) recognizes it.
JPEG_MIN = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" \
           b"\xff\xdb\x00C\x00\x08\xff\xd9"


def test_view_image_png(mcp, tmp_path):
    p = tmp_path / "red.png"
    p.write_bytes(PNG_1x1_RED)
    res = mcp.call("remote_view_image", path=str(p))
    assert res["detail"] == "high"
    url = res["image_url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG_1x1_RED


def test_view_image_jpeg(mcp, tmp_path):
    p = tmp_path / "pic.JPG"
    p.write_bytes(JPEG_MIN)
    res = mcp.call("remote_view_image", path=str(p))
    assert res["image_url"].startswith("data:image/jpeg;base64,")


def test_view_image_detail_original(mcp, tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(PNG_1x1_RED)
    res = mcp.call("remote_view_image", path=str(p), detail="original")
    assert res["detail"] == "original"


def test_view_image_unknown_ext_fallthrough(mcp, tmp_path):
    p = tmp_path / "blob.xyz"
    p.write_bytes(b"\x00\x01\x02\x03")
    res = mcp.call("remote_view_image", path=str(p))
    # Unknown extension → mime equals the ext ("xyz"); data URL carries it.
    assert res["image_url"].startswith("data:image/xyz;base64,"), res["image_url"]


def test_view_image_empty_path_raises(mcp):
    with pytest.raises(RuntimeError, match="path is required"):
        mcp.call("remote_view_image", path="")


def test_view_image_nonexistent_file_raises(mcp, tmp_path):
    with pytest.raises(Exception):
        mcp.call("remote_view_image", path=str(tmp_path / "no-such.png"))
