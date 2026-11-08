"""表情包未发出的原因行与下一轮通知车道。

不变量：
- 没发出去的键（解析失败与配额超限都算）永远带逐位平行的原因行。
- 轮末整表替换挂账（空表＝清掉遗留），通知只投一次就清，语义是「上一轮」。
- 通知复用既有 `[RUNTIME_NOTICE]` 消息且节排最前；复读剥离与截断撤账都认这一节。
"""

from __future__ import annotations

from pathlib import Path

from g3ku.runtime.skill_roster_notice import (
    RUNTIME_NOTICE_MARKER,
    STICKER_FAILURE_NOTICE_HEADING,
    STICKER_FAILURE_NOTICE_HINT,
    clear_skill_roster_notice_stores,
    context_has_state,
    deliver_for_scope,
    drop_records_after,
    normalize_sticker_failures,
    notice_context_for,
    pending_sticker_failures,
    record_sticker_failures,
    strip_skill_roster_notice_echo,
)
from g3ku.stickers import registry
from g3ku.stickers.marker import resolve_reply_stickers

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _pack(root: Path, *, count: int = 2) -> Path:
    target = root / "demo" / "happy"
    target.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        (target / f"raw-{index}.png").write_bytes(PNG + bytes([index]))
    registry.ensure_bootstrap(root / "demo")
    return root


def test_resolve_index_with_reason_reports_each_failure(tmp_path: Path):
    _pack(tmp_path)
    pack_dir = tmp_path / "demo"

    resolved, reason = registry.resolve_index_with_reason(pack_dir, "happy", 1)
    assert resolved is not None and reason == ""

    missing, reason = registry.resolve_index_with_reason(pack_dir, "happy", 9)
    assert missing is None and reason == "序号不存在"

    (pack_dir / "happy" / "002.png").unlink()
    gone, reason = registry.resolve_index_with_reason(pack_dir, "happy", 2)
    assert gone is None and reason == "图片文件不存在"

    (pack_dir / "happy" / "001.png").write_bytes(PNG + b"swapped")
    swapped, reason = registry.resolve_index_with_reason(pack_dir, "happy", 1)
    assert swapped is None and reason == "内容已变更"


def test_undelivered_keys_carry_parallel_reasons(tmp_path: Path):
    _pack(tmp_path)

    reply = resolve_reply_stickers(
        "[[sticker:demo/sad]]\n"
        "[[sticker:ghost/happy]]\n"
        "[[sticker:demo/happy/9]]\n"
        "[[sticker:not a marker]]\n"
        "[[sticker:missing_cat]]\n",
        root=tmp_path,
    )
    assert reply.undelivered == (
        "demo/sad",
        "ghost/happy",
        "demo/happy/9",
        "not a marker",
        "missing_cat",
    )
    assert reply.undelivered_reasons == (
        "分类不存在",
        "包不存在",
        "序号不存在",
        "标记语法不识别",
        "分类不存在或多个包重名",
    )
    assert len(reply.undelivered) == len(reply.undelivered_reasons)


def test_cap_exceeded_markers_are_reported_with_the_cap_reason(tmp_path: Path):
    _pack(tmp_path, count=3)

    reply = resolve_reply_stickers(
        "[[sticker:demo/happy]]\n[[sticker:demo/happy]]\n",
        root=tmp_path,
        max_per_reply=1,
    )
    assert len(reply.delivered) == 1
    assert reply.undelivered == ("demo/happy",)
    assert reply.undelivered_reasons == ("超出每轮发送上限",)

    # cap=0 是停发开关：全部标记都因超限没出去，通知同样逐条点名。
    zero = resolve_reply_stickers("[[sticker:demo/happy]]", root=tmp_path, max_per_reply=0)
    assert zero.delivered == () and zero.undelivered == ("demo/happy",)
    assert zero.undelivered_reasons == ("超出每轮发送上限",)


def test_failure_notice_delivers_once_and_clears():
    clear_skill_roster_notice_stores()
    scope = "session:ext:test"
    record_sticker_failures(
        scope,
        [
            {"key": "demo/shy", "reason": "分类不存在"},
            {"key": "demo/happy/9", "reason": "序号不存在"},
            # 同键重复行按 key 去重
            {"key": "demo/shy", "reason": "重复行"},
        ],
    )
    rows = pending_sticker_failures(notice_context_for(scope))
    assert [row["key"] for row in rows] == ["demo/shy", "demo/happy/9"]

    message, delivered = deliver_for_scope(scope, [], now="2026-11-09T00:00:00+08:00")
    assert message is not None
    assert delivered == []
    content = str(message["content"])
    assert content.startswith(RUNTIME_NOTICE_MARKER)
    assert STICKER_FAILURE_NOTICE_HEADING in content
    assert "- `demo/shy` — 分类不存在" in content
    assert "- `demo/happy/9` — 序号不存在" in content
    assert STICKER_FAILURE_NOTICE_HINT in content
    # 节排最前：抬头之后第一个小节就是它
    body = content[len(RUNTIME_NOTICE_MARKER):].lstrip()
    assert body.startswith(STICKER_FAILURE_NOTICE_HEADING)

    assert pending_sticker_failures(notice_context_for(scope)) == []
    again, _delivered = deliver_for_scope(scope, [], now="2026-11-09T00:01:00+08:00")
    assert again is None


def test_record_replaces_pending_table_each_turn():
    clear_skill_roster_notice_stores()
    scope = "session:ext:test"
    record_sticker_failures(scope, [{"key": "a/b", "reason": "包不存在"}])
    # 空表＝上一轮没有失败：清掉遗留挂账，而不是攒成历史清单
    record_sticker_failures(scope, [])
    assert pending_sticker_failures(notice_context_for(scope)) == []

    record_sticker_failures(scope, [{"key": "c/d", "reason": "序号不存在"}])
    rows = pending_sticker_failures(notice_context_for(scope))
    assert rows == [{"key": "c/d", "reason": "序号不存在"}]


def test_failure_rows_are_capped_and_trimmed():
    rows = normalize_sticker_failures(
        [{"key": f"k/{index}", "reason": "x"} for index in range(20)]
    )
    assert len(rows) == 8
    long = normalize_sticker_failures([{"key": "k", "reason": "r" * 300}])
    assert len(long[0]["reason"]) == 160
    assert normalize_sticker_failures([{"key": "", "reason": "x"}, "junk"]) == []


def test_echo_strip_recognizes_sticker_section():
    notice = "\n".join(
        [
            RUNTIME_NOTICE_MARKER,
            STICKER_FAILURE_NOTICE_HEADING,
            "- `demo/shy` — 分类不存在",
        ]
    )
    text = f"先回答用户的问题。\n\n{notice}"
    assert strip_skill_roster_notice_echo(text) == "先回答用户的问题。"


def test_truncate_clears_pending_and_state_detection_sees_it():
    clear_skill_roster_notice_stores()
    payload = {"sticker_failures_pending": [{"key": "a/b", "reason": "包不存在"}]}
    assert context_has_state(payload) is True

    dropped = drop_records_after(payload, cutoff_iso="")
    assert dropped["sticker_failures_pending"] == []
    kept = drop_records_after(payload, cutoff_iso="2026-11-09T00:00:00+08:00")
    # 挂账对应的那条回复已随截断回卷：两种形状都要清，不能把不存在的历史当上一轮
    assert kept["sticker_failures_pending"] == []
