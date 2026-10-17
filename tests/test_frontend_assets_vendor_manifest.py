"""资源探测到期时对跟踪 manifest 的写入。

`vendor/*-manifest.json` 是 git 里的文件：一次"内容没变"的空重写就足以让设备
永久假脏，而升级守卫读的就是这份脏。
"""

from pathlib import Path

from g3ku.web.frontend_assets import _write_manifest

_PAYLOAD = {"asset_id": "lucide", "installed_version": "1.52.0", "bundle": "lucide.min.js"}


def test_identical_content_does_not_rewrite_the_tracked_file(tmp_path: Path):
    target = tmp_path / "vendor" / "lucide-manifest.json"
    _write_manifest(target, _PAYLOAD)
    written = target.read_bytes()
    assert b"\r\n" not in written

    # git 在 autocrlf 设备上检出的是 CRLF：字节相同、行尾不同也不算"需要重写"。
    target.write_bytes(written.replace(b"\n", b"\r\n"))
    _write_manifest(target, _PAYLOAD)
    assert target.read_bytes() == written.replace(b"\n", b"\r\n"), "空重写会把跟踪文件变成假脏"


def test_real_change_still_writes(tmp_path: Path):
    target = tmp_path / "vendor" / "lucide-manifest.json"
    _write_manifest(target, _PAYLOAD)
    _write_manifest(target, {**_PAYLOAD, "installed_version": "1.59.0"})
    text = target.read_text(encoding="utf-8")
    assert "1.59.0" in text
    assert b"\r\n" not in target.read_bytes()
