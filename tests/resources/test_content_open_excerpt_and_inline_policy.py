from __future__ import annotations

from pathlib import Path

from g3ku.content.navigation import (
    _ALWAYS_INLINE_TOOL_RESULT_SOURCES,
    CHAR_MODE_OPEN_CHAR_LIMIT,
    INLINE_CHAR_LIMIT,
    LINE_GUTTER_SEPARATOR,
    OPEN_EXCERPT_CHAR_LIMIT,
    SEARCH_PREVIEW_CHAR_LIMIT,
    ContentNavigationService,
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
    assert result.get("excerpt") == "1: 第一行\n2: 第二行"


def test_open_excerpt_line_numbers_are_absolute_across_windows(tmp_path: Path) -> None:
    # 行号栏用文件的绝对行号，不是"窗口内第几行"：被引 343 就必须显示 343，
    # 否则打开窗口的车道与 rg -n 又回到两个数上（2026-10-16 task:7170f1df65a3 的漂移来源）。
    service = _make_service(tmp_path)
    path = tmp_path / "numbered.txt"
    path.write_text("\n".join("body-%03d" % i for i in range(1, 61)), encoding="utf-8")

    result = service.open(path=str(path), start_line=40, end_line=42)

    assert result.get("excerpt") == "40: body-040\n41: body-041\n42: body-042"
    assert result.get("start_line") == 40 and result.get("end_line") == 42


def test_open_char_mode_excerpt_stays_unnumbered(tmp_path: Path) -> None:
    # 字符模式切的不是整行，给它标"行号"会标出一个假事实。
    service = _make_service(tmp_path)
    path = tmp_path / "single.txt"
    path.write_text("a" * 30_000, encoding="utf-8")

    result = service.open(path=str(path), start_char=1, end_char=40)

    assert result.get("excerpt") == "a" * 40


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
    # 行对齐：去掉行号栏之后的每一行都是完整 200 字符，没有半行。
    assert all(len(line.split(LINE_GUTTER_SEPARATOR, 1)[1]) == 200 for line in shown_lines)
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


# 契约变更（2026-11-12，维护者批准的档 3）：旧合同是"两族同时在场即 ValueError"，
# 新合同是"行寻址优先、字符族丢弃并在回执里回显 selector_conflict"。本用例由旧合同
# 的互斥断言改写而来，不是顺手修掉一条陈旧断言：容忍是运行时的事，模型面仍只说用一族。
def test_open_char_and_line_selectors_resolve_to_line_addressing(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    path = tmp_path / "plain.txt"
    path.write_text("\n".join(f"l{index}" for index in range(1, 11)), encoding="utf-8")

    result = service.open(path=str(path), start_line=2, end_line=4, start_char=1, end_char=30_000)

    assert result.get("ok") is True
    assert result.get("start_line") == 2 and result.get("end_line") == 4
    assert result.get("excerpt") == "2: l2\n3: l3\n4: l4"
    assert result.get("selector_conflict") == {
        "used": "line",
        "discarded": {"start_char": 1, "end_char": 30_000},
    }


def test_open_single_selector_family_keeps_the_baseline_shape(tmp_path: Path) -> None:
    """单族与无寻址调用不新增任何字段：消歧落点只在真发生冲突时出现。"""
    service = _make_service(tmp_path)
    path = tmp_path / "plain.txt"
    path.write_text("\n".join(f"l{index}" for index in range(1, 6)), encoding="utf-8")

    line_only = service.open(path=str(path), start_line=2, end_line=3)
    char_only = service.open(path=str(path), start_char=1, end_char=2)
    no_selector = service.open(path=str(path))

    assert "selector_conflict" not in line_only
    assert "selector_conflict" not in char_only
    assert "selector_conflict" not in no_selector
    assert line_only.get("excerpt") == "2: l2\n3: l3"
    assert char_only.get("excerpt") == "l1"
    assert no_selector.get("excerpt").startswith("1: l1")


def test_open_line_priority_with_end_line_one_stays_discoverable(tmp_path: Path) -> None:
    """A 档已知退化：行区间只取到一行时，事实仍可从 discarded 与整文件读数看出。

    `truncated:false` 说的是"你请求的行区间给全了"，不是"你要的字符区间给全了"；
    运行时不替模型猜意图，也不把这一条当"已告知少拿"。
    """
    service = _make_service(tmp_path)
    path = tmp_path / "plain.txt"
    path.write_text("\n".join(f"l{index}" for index in range(1, 51)), encoding="utf-8")

    result = service.open(path=str(path), start_line=1, end_line=1, start_char=1, end_char=20_000)

    assert result.get("ok") is True
    assert result.get("start_line") == 1 and result.get("end_line") == 1
    assert result.get("truncated") is False
    assert result.get("excerpt_remaining_lines") == 0
    assert result.get("selector_conflict", {}).get("discarded", {}).get("end_char") == 20_000
    assert result.get("line_count") == 50
    assert int(result.get("char_count") or 0) > 0


def test_open_mixed_selectors_single_line_falls_back_to_char_truncation(tmp_path: Path) -> None:
    """丢弃字符族后命中单行超长：走行模式的字符兜底，是"更小摘录 + 续读指引"。"""
    service = _make_service(tmp_path)
    path = tmp_path / "one-line.txt"
    path.write_text("x" * 30_000, encoding="utf-8")

    result = service.open(path=str(path), start_line=1, end_line=1, start_char=1, end_char=30_000)

    assert result.get("ok") is True
    assert result.get("truncated") is True
    assert result.get("selector_conflict", {}).get("used") == "line"
    assert "start_char" in (result.get("excerpt") or "")


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


def _model_facing_tool_names() -> set[str]:
    names: set[str] = set()
    for manifest in Path(__file__).resolve().parents[2].glob("tools/*/resource.yaml"):
        for line in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("name:"):
                names.add(line.split(":", 1)[1].strip())
                break
    return names


def test_always_inline_sources_are_model_facing_names() -> None:
    # 交付侧按 `tool_result:<模型面名>` 建串（_ceo_support.py、react_loop.py），
    # 写成资源目录名（*_cn）就静默永不命中——2026-10-11 那份 7 MB 节点详情正是
    # 因为没命中才被外置的，所以这条既要防拼错，也不能让白名单无上限地内联。
    names = _model_facing_tool_names()
    assert names, "tools/*/resource.yaml should expose model-facing names"
    for source in _ALWAYS_INLINE_TOOL_RESULT_SOURCES:
        assert source.removeprefix("tool_result:") in names, source


def test_always_inline_exception_inlines_mid_size_but_not_giant_payloads() -> None:
    mid_size = "x" * (INLINE_CHAR_LIMIT + 2_000)
    assert tool_result_delivery_policy(
        mid_size,
        source_kind="tool_result:task_node_detail",
    ) == "inline_small"

    giant = "x" * (OPEN_EXCERPT_CHAR_LIMIT + 1)
    assert tool_result_delivery_policy(
        giant,
        source_kind="tool_result:task_node_detail",
    ) == "summary_with_ref"
