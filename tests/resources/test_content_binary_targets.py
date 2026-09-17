"""二进制目标的 content 工具契约回归。

背景（2026-09-17 task:77d0ae460cf0 验收误判）：content 工具把二进制文件的整份内容替换为
占位串 "[二进制文件：X]"，而 describe 的 size_bytes、摘要里的 "N lines, M chars" 都是**占位串**的
统计。验收节点据此把 629KB 的合法 PDF 判成"34 字节空壳"，并指控执行节点伪造审计证据。
本文件锁定修复后的契约：真实体积来自磁盘、占位文本被显式标记、二进制搜索走字节层。
"""
from __future__ import annotations

from pathlib import Path

import g3ku.content.navigation as navigation
from g3ku.content.navigation import ContentNavigationService

_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n" + bytes(range(256)) * 4 + b"\n%%EOF\n"


def _make_service(tmp_path: Path) -> ContentNavigationService:
    return ContentNavigationService(workspace=tmp_path, allowed_dir=tmp_path)


def _write_binary(tmp_path: Path, name: str = "resume.pdf", payload: bytes = _PDF_BYTES) -> Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def test_binary_target_reports_real_file_size_not_placeholder_length(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = _write_binary(tmp_path)

    result = service.describe(path=str(path))

    assert result["size_bytes"] == path.stat().st_size
    assert result["content_display_replaced"] is True
    assert result["mime_type"] == "application/pdf"
    # 摘要必须给出磁盘真实体积，且不得再把占位串的字符数当作文件事实呈现。
    assert f"{path.stat().st_size} bytes on disk" in result["summary"]
    assert f"({result['line_count']} lines, {result['char_count']} chars)" not in result["summary"]


def test_binary_target_handle_carries_placeholder_flag(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = _write_binary(tmp_path)

    payload = service.describe(path=str(path))["handle"]

    assert payload["content_display_replaced"] is True
    assert payload["size_bytes"] == path.stat().st_size


def test_open_payload_for_binary_carries_size_and_notice(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = _write_binary(tmp_path)

    result = service.open(path=str(path))

    assert result["excerpt"] == "[二进制文件：resume.pdf]"
    assert result["binary"] is True
    assert result["content_display_replaced"] is True
    assert result["size_bytes"] == path.stat().st_size
    assert "placeholder" in result["notice"]
    # 外部化预览只取载荷头部：真实体积必须靠前，不能被挤到看不见的位置。
    early_keys = list(result.keys())[:14]
    assert "size_bytes" in early_keys


def test_image_target_is_flagged_as_binary_with_real_size(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = _write_binary(tmp_path, name="photo.png", payload=b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3)

    result = service.describe(path=str(path))

    assert result["content_display_replaced"] is True
    assert result["size_bytes"] == path.stat().st_size
    assert result["mime_type"] == "image/png"


def test_search_on_binary_target_finds_signature_at_byte_level(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = _write_binary(tmp_path)

    result = service.search(path=str(path), query="%PDF")

    # 修复前：在占位串里搜索 → 对任何有效 PDF 恒为 0 命中（"无 %PDF 头"的假阴性来源）。
    assert result["count"] >= 1
    assert result["byte_level"] is True
    assert result["size_bytes"] == path.stat().st_size
    assert result["hits"][0]["byte_offset"] == 0
    assert result["line_count"] == 0 and result["char_count"] == 0


def test_binary_search_reports_each_match_once_across_chunk_boundary(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    # 缩小分块，让签名横跨块边界，验证 overlap 既不漏报也不重复上报。
    original_chunk = navigation._BINARY_SEARCH_CHUNK_BYTES
    navigation._BINARY_SEARCH_CHUNK_BYTES = 64
    try:
        # 必须含非 UTF-8 字节才会被判为二进制目标（纯 ASCII 载荷走文本路径，属正确行为）。
        payload = b"\xff\xfe" + b"A" * 58 + b"NEEDLE" + b"B" * 100 + b"NEEDLE" + b"C" * 30
        path = _write_binary(tmp_path, name="chunked.bin", payload=payload)
        result = service.search(path=str(path), query="NEEDLE")
    finally:
        navigation._BINARY_SEARCH_CHUNK_BYTES = original_chunk

    offsets = [hit["byte_offset"] for hit in result["hits"]]
    assert offsets == [60, 166]


def test_text_target_keeps_text_semantics_and_lean_payload(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "plain.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8")

    described = service.describe(path=str(path))
    opened = service.open(path=str(path))
    searched = service.search(path=str(path), query="beta")

    assert described["content_display_replaced"] is False
    assert described["size_bytes"] == path.stat().st_size
    assert described["summary"].startswith("Externalized ")
    # 文本目标保持精简 open 契约（不加二进制元数据）。
    for absent in ("binary", "size_bytes", "mime_type", "notice"):
        assert absent not in opened
    assert searched["hits"][0]["line"] == 2
    assert "byte_level" not in searched
