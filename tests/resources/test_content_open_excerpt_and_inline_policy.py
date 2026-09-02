from __future__ import annotations

from pathlib import Path

from g3ku.content.navigation import (
    ContentNavigationService,
    OPEN_EXCERPT_CHAR_LIMIT,
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
    assert "content_search" in excerpt


def test_open_excerpt_small_content_not_truncated(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "small.txt"
    path.write_text("第一行\n第二行\n第三行", encoding="utf-8")

    result = service.open(path=str(path), start_line=1, end_line=2)

    assert result.get("ok") is True
    assert result.get("truncated") is False
    assert result.get("excerpt") == "第一行\n第二行"