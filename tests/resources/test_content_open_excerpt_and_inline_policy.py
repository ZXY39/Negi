from __future__ import annotations

from pathlib import Path

from g3ku.content.navigation import (
    CHAR_MODE_OPEN_CHAR_LIMIT,
    ContentNavigationService,
    OPEN_EXCERPT_CHAR_LIMIT,
    SEARCH_PREVIEW_CHAR_LIMIT,
    tool_result_delivery_policy,
)


def _make_service(tmp_path: Path) -> ContentNavigationService:
    return ContentNavigationService(workspace=tmp_path, allowed_dir=tmp_path)


def _content_open_payload(excerpt: str) -> dict:
    return {
        "ok": True,
        "ref": "artifact:demo",
        "resolved_ref": "artifact:demo",
        "start_line": 1,
        "end_line": 1,
        "excerpt": excerpt,
    }


def test_content_open_small_excerpt_opens_inline() -> None:
    payload = _content_open_payload("short excerpt")
    assert tool_result_delivery_policy(payload, source_kind="tool_result:content_open") == "inline_small"


def test_content_open_single_giant_line_is_not_kept_inline() -> None:
    # Regression: content_open 曾无视字符规模把节选判定为 inline_small，
    # 单行巨型 artifact 会把整行内容带进请求体/持久基线。
    payload = _content_open_payload("x" * 2_000_000)
    assert tool_result_delivery_policy(payload, source_kind="tool_result:content_open") == "summary_with_ref"


def test_content_open_missing_line_fields_falls_back_to_generic_policy() -> None:
    payload = _content_open_payload("short excerpt")
    payload.pop("end_line")
    # 行号字段缺失时不走 content_open 专属内联分支，落回通用大小策略（小负载 inline）。
    assert tool_result_delivery_policy(payload, source_kind="tool_result:content_open") == "inline_small"


def test_open_excerpt_truncates_oversized_single_line(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    big_line = "大" * 30_000
    path = tmp_path / "single-line-artifact.txt"
    path.write_text(big_line, encoding="utf-8")

    result = service.open(path=str(path), start_line=1, end_line=1)

    assert result.get("ok") is True
    assert result.get("truncated") is True
    assert result.get("excerpt_total_chars") == 30_000
    excerpt = result.get("excerpt") or ""
    assert len(excerpt) <= OPEN_EXCERPT_CHAR_LIMIT
    assert "内容已截断" in excerpt
    # 单行超长内容：行参数无效，提示改用 start_char 字符分页并给出 exec 备选；
    # 不再推荐对单行同样失效的 content_search。
    assert "start_char" in excerpt
    assert "exec" in excerpt


def test_open_excerpt_small_content_not_truncated(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "small.txt"
    path.write_text("第一行\n第二行\n第三行", encoding="utf-8")

    result = service.open(path=str(path), start_line=1, end_line=2)

    assert result.get("ok") is True
    assert result.get("truncated") is False
    assert result.get("excerpt") == "第一行\n第二行"


def test_open_default_reads_small_file_in_full(tmp_path: Path) -> None:
    # 取消默认 80 行：无参数时按字符上限行对齐取整，小文件一次读全。
    service = _make_service(tmp_path)
    path = tmp_path / "small-many-lines.txt"
    path.write_text("\n".join(f"line-{index:03d}" for index in range(1, 201)), encoding="utf-8")

    result = service.open(path=str(path))

    assert result.get("ok") is True
    assert result.get("truncated") is False
    assert result.get("start_line") == 1
    assert result.get("end_line") == 200


def test_open_default_line_aligned_truncates_without_splitting_line(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "dense.txt"
    path.write_text("\n".join("x" * 200 for _ in range(200)), encoding="utf-8")

    result = service.open(path=str(path))

    assert result.get("ok") is True
    assert result.get("truncated") is True
    shown_lines = (result.get("excerpt") or "").split("\n\n[内容已截断]")[0].splitlines()
    # 行对齐：显示的每一行都是完整 200 字符，没有半行。
    assert all(len(line) == 200 for line in shown_lines)
    assert result.get("excerpt_shown_chars") <= OPEN_EXCERPT_CHAR_LIMIT
    assert "start_line=" in result.get("excerpt")


def test_open_char_mode_paginates_single_line(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "single.txt"
    path.write_text("a" * 30_000, encoding="utf-8")

    first = service.open(path=str(path), start_char=1, end_char=20_000)
    assert first.get("ok") is True
    assert first.get("truncated") is False
    assert first.get("start_char") == 1
    assert first.get("end_char") == 20_000

    second = service.open(path=str(path), start_char=20_001, end_char=30_000)
    assert second.get("ok") is True
    assert second.get("truncated") is False
    assert second.get("start_char") == 20_001
    assert second.get("excerpt_shown_chars") == 10_000


def test_open_char_mode_caps_oversized_window(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "huge-single.txt"
    path.write_text("b" * (CHAR_MODE_OPEN_CHAR_LIMIT + 50_000), encoding="utf-8")

    result = service.open(path=str(path), start_char=1, end_char=CHAR_MODE_OPEN_CHAR_LIMIT + 50_000)

    assert result.get("ok") is True
    assert result.get("truncated") is True
    assert result.get("excerpt_shown_chars") <= CHAR_MODE_OPEN_CHAR_LIMIT
    assert "start_char=" in result.get("excerpt")


def test_open_char_and_line_selectors_are_mutually_exclusive(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "any.txt"
    path.write_text("line\n", encoding="utf-8")

    try:
        service.open(path=str(path), start_line=1, start_char=1)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for mixed line/char selectors")


def test_tail_single_line_returns_tail_not_head(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "single-tail.txt"
    path.write_text("a" * 20_000 + "TAILMARKER", encoding="utf-8")

    result = service.tail(path=str(path))

    assert result.get("ok") is True
    assert "TAILMARKER" in result.get("excerpt")


def test_open_payload_is_lean_and_keeps_top_level_refs(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "lean.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8")

    result = service.open(path=str(path))

    # 裁剪：不再有嵌套 handle / 预览 / 内部字段。
    for removed in ("handle", "head_preview", "tail_preview", "artifact_id", "uri", "size_bytes", "mime_type"):
        assert removed not in result
    # 保留：顶层 ref 家族 + source_kind/line_count/char_count（供外部化溯源与分页）。
    for kept in ("ref", "requested_ref", "resolved_ref", "wrapper_ref", "wrapper_depth", "source_kind", "line_count", "char_count", "start_line", "end_line", "excerpt", "truncated"):
        assert kept in result


def test_char_mode_excerpt_inlines_even_with_metadata(tmp_path: Path) -> None:
    # 元数据不计入内容字符上限：char 模式的大正文（<=128000）即便序列化后更大也应内联。
    payload = {
        "ok": True,
        "ref": "artifact:demo",
        "resolved_ref": "artifact:demo",
        "start_line": 1,
        "end_line": 1,
        "start_char": 1,
        "end_char": 60_000,
        "excerpt": "x" * 60_000,
    }
    assert tool_result_delivery_policy(payload, source_kind="tool_result:content_open") == "inline_small"


def test_search_preview_is_capped_for_single_line_match(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "single-search.txt"
    path.write_text("needle " + "y" * 5_000, encoding="utf-8")

    result = service.search(path=str(path), query="needle")

    assert result.get("ok") is True
    assert result.get("count") == 1
    assert len(result["hits"][0]["preview"]) <= SEARCH_PREVIEW_CHAR_LIMIT + 1
    assert "handle" not in result