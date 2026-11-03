"""内容审查词替换：词表规范、匹配、内容形态、防自噬与缓存。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from g3ku.deployment import data_root as data_root_module
from g3ku.security import content_policy as cp


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("G3KU_DATA_DIR", str(tmp_path))
    data_root_module.reset_cache()
    cp.load_policy(force=True)
    try:
        yield tmp_path
    finally:
        data_root_module.reset_cache()


def _matcher(words):
    return cp.compile_matcher(words)


def test_normalize_words_handles_crlf_bom_blank_duplicates_and_commas() -> None:
    text = "\ufeffWordA\r\n\r\n  wordb  \nWORDA\nsix,four\n卐\n"
    assert cp.normalize_words(text) == ["WordA", "wordb", "six,four", "卐"]


def test_format_words_round_trips_normalized_list() -> None:
    assert cp.format_words(["b", "a", "B"]) == "b\na\n"


def test_policy_file_missing_means_disabled_and_empty() -> None:
    policy = cp.load_policy()
    assert policy == {
        "version": 1,
        "enabled": False,
        "words": [],
        "updated_at": "",
    }
    assert not cp.policy_path().exists()


def test_save_policy_is_atomic_and_readable() -> None:
    saved = cp.save_policy(True, [" 六四 ", "", "六四", "卐"])
    assert saved["enabled"] is True
    assert saved["words"] == ["六四", "卐"]
    assert saved["updated_at"]
    raw = json.loads(cp.policy_path().read_text(encoding="utf-8"))
    assert raw["enabled"] is True
    assert raw["words"] == ["六四", "卐"]
    assert not list(cp.policy_path().parent.glob("content-policy.json.tmp"))
    assert cp.load_policy() == saved


def test_load_policy_invalidates_on_file_change(tmp_path: Path) -> None:
    cp.save_policy(True, ["alpha"])
    assert cp.load_policy()["words"] == ["alpha"]
    cp.policy_path().write_text(
        json.dumps({"version": 1, "enabled": False, "words": ["beta-longer"]}, ensure_ascii=False),
        encoding="utf-8",
    )
    policy = cp.load_policy()
    assert policy["enabled"] is False
    assert policy["words"] == ["beta-longer"]


def test_replace_text_is_case_insensitive_and_counts_occurrences() -> None:
    matcher = _matcher(["WordA"])
    replaced, hits = cp.replace_text("worda WORDA WordA", matcher)
    assert replaced == " ".join([cp.REPLACEMENT_MARKER] * 3)
    assert hits == 3


def test_replace_text_prefers_long_words() -> None:
    matcher = _matcher(["b", "abcd"])
    replaced, hits = cp.replace_text("abcd", matcher)
    assert replaced == cp.REPLACEMENT_MARKER
    assert hits == 1


def test_replace_text_supports_single_character_words() -> None:
    matcher = _matcher(["卐"])
    replaced, hits = cp.replace_text("a卐b", matcher)
    assert replaced == f"a{cp.REPLACEMENT_MARKER}b"
    assert hits == 1


def test_replace_text_empty_matcher_is_noop() -> None:
    assert cp.compile_matcher([]) is None
    assert cp.replace_text("六四", None) == ("六四", 0)


def test_replace_text_protects_marker_and_notice_spans() -> None:
    matcher = _matcher(["违规"])
    text = f"{cp.REPLACEMENT_MARKER}\n{cp.NOTICE_PREFIX} 已替换 1 处 违规\n违规"
    replaced, hits = cp.replace_text(text, matcher)
    assert replaced.startswith(cp.REPLACEMENT_MARKER)
    assert f"{cp.NOTICE_PREFIX} 已替换 1 处 违规" in replaced
    assert replaced.endswith(cp.REPLACEMENT_MARKER)
    assert hits == 1


def test_replace_content_json_dict_replaces_values_keeps_keys_and_puts_notice_first() -> None:
    matcher = _matcher(["六四"])
    content = json.dumps({"六四": "正文 六四", "nested": {"text": "六四"}}, ensure_ascii=False)
    replaced, hits = cp.replace_content(
        content, matcher, notice=True, revision=cp.policy_revision()
    )
    parsed = json.loads(replaced)
    assert list(parsed.keys())[0] == cp.NOTICE_FIELD
    assert "已替换 2 处" in parsed[cp.NOTICE_FIELD]
    assert parsed["六四"] == f"正文 {cp.REPLACEMENT_MARKER}"
    assert parsed["nested"]["text"] == cp.REPLACEMENT_MARKER
    assert hits == 2


def test_replace_content_json_list_prefixes_notice_and_keeps_json_body() -> None:
    matcher = _matcher(["六四"])
    replaced, hits = cp.replace_content(
        '["六四", "safe"]', matcher, notice=True, revision=cp.policy_revision()
    )
    first, body = replaced.split("\n", 1)
    assert first.startswith(cp.NOTICE_PREFIX)
    assert json.loads(body) == [cp.REPLACEMENT_MARKER, "safe"]
    assert hits == 1


def test_replace_content_plain_text_prefixes_notice_on_first_line() -> None:
    matcher = _matcher(["六四"])
    replaced, hits = cp.replace_content(
        "line1 六四\nline2", matcher, notice=True, revision=cp.policy_revision()
    )
    first, rest = replaced.split("\n", 1)
    assert first.startswith(cp.NOTICE_PREFIX)
    assert rest == f"line1 {cp.REPLACEMENT_MARKER}\nline2"
    assert hits == 1


def test_replace_content_without_hits_returns_exact_original() -> None:
    matcher = _matcher(["六四"])
    original = '{"a": 1,   "b": "safe"}'
    assert cp.replace_content(original, matcher, notice=True) == (original, 0)


def test_replace_content_does_not_add_second_notice() -> None:
    matcher = _matcher(["六四"])
    content = json.dumps(
        {cp.NOTICE_FIELD: "已有提示 六四", "text": "六四"},
        ensure_ascii=False,
    )
    replaced, hits = cp.replace_content(
        content, matcher, notice=True, revision=cp.policy_revision()
    )
    parsed = json.loads(replaced)
    assert parsed[cp.NOTICE_FIELD] == "已有提示 六四"
    assert list(parsed).count(cp.NOTICE_FIELD) == 1
    assert parsed["text"] == cp.REPLACEMENT_MARKER
    assert hits == 1


def test_apply_content_policy_masks_tool_rows_and_user_rows_without_mutating() -> None:
    cp.save_policy(True, ["六四"])
    messages = [
        {"role": "user", "content": "六四"},
        {"role": "tool", "content": "old 六四"},
        {"role": "assistant", "content": "ok"},
        {"role": "tool", "content": "new 六四"},
        {"role": "tool", "content": "newer 六四"},
    ]
    before = [dict(item) for item in messages]
    result = cp.apply_content_policy(messages)
    assert messages == before
    # 发起方正文同样要换：用户自己打来的词是上游拦下的主来源，只处理工具结果救不活这种会话。
    assert result[0]["content"] == cp.REPLACEMENT_MARKER
    assert cp.NOTICE_PREFIX not in result[0]["content"]
    assert result[1]["content"] == f"old {cp.REPLACEMENT_MARKER}"
    assert cp.NOTICE_PREFIX not in result[1]["content"]
    # assistant 消息把旧工具结果和当前 hop 隔开：只有末尾连续两条加提示。
    assert result[3]["content"].startswith(cp.NOTICE_PREFIX)
    assert result[4]["content"].startswith(cp.NOTICE_PREFIX)
    # 模型自己说过的话不换：改了它对不上自己的历史。
    assert result[2]["content"] == "ok"


def test_apply_content_policy_masks_text_parts_of_multimodal_user_message() -> None:
    cp.save_policy(True, ["六四", "法轮功"])
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "六四 和 法轮功"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
            ],
        }
    ]
    result = cp.apply_content_policy(messages)
    parts = result[0]["content"]
    assert parts[0] == {"type": "text", "text": f"{cp.REPLACEMENT_MARKER} 和 {cp.REPLACEMENT_MARKER}"}
    assert cp.NOTICE_PREFIX not in json.dumps(parts, ensure_ascii=False)
    # 图片部件原样：文本替换管不到图片触发的拦截。
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64,AAAA"


def test_user_row_notice_goes_out_on_the_notice_lane_not_inside_the_row() -> None:
    """掩掉发起方正文后的说明走 `[RUNTIME_NOTICE]`，不写进那条消息里。

    写进行内会被读成"用户自己发了一句系统提醒"——那正是要避免的误读形状；说明也只挂在
    这一发的副本上，不进 durable 基线。
    """
    cp.save_policy(True, ["六四"])
    messages = [{"role": "user", "content": "六四 是什么"}]
    masked = cp.apply_content_policy(messages)
    assert cp.count_user_row_markers(masked) - cp.count_user_row_markers(messages) == 1
    assert cp.NOTICE_PREFIX not in masked[0]["content"]

    with_notice = cp.append_content_policy_notice(masked, 1)
    assert len(with_notice) == 2
    notice = with_notice[1]["content"]
    assert notice.startswith(cp.RUNTIME_NOTICE_MARKER)
    assert "安全审查词替换" in notice and "1 处" in notice
    # 已有通知时并进去，不再新起一条。
    merged = cp.append_content_policy_notice(with_notice, 2)
    assert len(merged) == 2
    assert merged[1]["content"].count(cp.RUNTIME_NOTICE_MARKER) == 1
    assert "2 处" in merged[1]["content"]


def test_apply_content_policy_never_rewrites_assistant_or_system_rows() -> None:
    cp.save_policy(True, ["六四"])
    messages = [
        {"role": "system", "content": "契约里写着 六四"},
        {"role": "assistant", "content": "我上次答过 六四"},
    ]
    assert cp.apply_content_policy(messages) == messages


def test_apply_content_policy_adds_notice_to_current_hop() -> None:
    cp.save_policy(True, ["六四"])
    messages = [
        {"role": "tool", "content": "old 六四"},
        {"role": "assistant", "content": "call tool"},
        {"role": "tool", "content": "new 六四"},
        {"role": "tool", "content": "newer 六四"},
    ]
    result = cp.apply_content_policy(messages)
    assert cp.NOTICE_PREFIX not in result[0]["content"]
    assert result[2]["content"].startswith(cp.NOTICE_PREFIX)
    assert result[3]["content"].startswith(cp.NOTICE_PREFIX)


def test_apply_content_policy_notices_tools_before_a_trailing_runtime_notice() -> None:
    cp.save_policy(True, ["六四"])
    messages = [
        {"role": "assistant", "content": "call tool"},
        {"role": "tool", "content": "new 六四"},
        {"role": "user", "content": "[RUNTIME_NOTICE] 合同更新"},
    ]
    result = cp.apply_content_policy(messages)
    assert result[1]["content"].startswith(cp.NOTICE_PREFIX)


def test_apply_content_policy_keeps_non_string_tool_content() -> None:
    cp.save_policy(True, ["六四"])
    messages = [
        {"role": "tool", "content": [{"type": "text", "text": "六四"}]},
        {"role": "assistant", "content": "六四"},
    ]
    result = cp.apply_content_policy(messages)
    assert result == messages


def test_apply_content_policy_disabled_returns_unchanged() -> None:
    cp.save_policy(False, ["六四"])
    messages = [{"role": "tool", "content": "六四"}]
    assert cp.apply_content_policy(messages) == messages


def test_mask_cache_is_keyed_by_content_and_revision() -> None:
    cp.save_policy(True, ["alpha"])
    messages = [{"role": "tool", "content": "alpha"}]
    first = cp.apply_content_policy(messages)
    cache_size = len(cp._MASK_CACHE)
    assert cache_size >= 1
    second = cp.apply_content_policy(messages)
    assert second == first
    assert len(cp._MASK_CACHE) == cache_size

    cp.save_policy(True, ["beta"])
    third = cp.apply_content_policy(messages)
    assert third[0]["content"] == "alpha"
    assert len(cp._MASK_CACHE) > cache_size
