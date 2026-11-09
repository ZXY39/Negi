"""渠道回复里没发出去的本地媒体：原因行与下一轮通知车道。

不变量：
- 引用语法成立、但文件不存在或在允许目录之外，逐条带原因挂账。
- 与表情包失败同一条 `[RUNTIME_NOTICE]` 消息，媒体一节紧随表情包之后。
- 并入而非整表替换（回合回复与主动推送没有统一的"轮"收口点），按引用去重、
  投一次就清；截断回卷一并撤账。
"""

from __future__ import annotations

from pathlib import Path

from g3ku.runtime.skill_roster_notice import (
    MEDIA_FAILURE_NOTICE_HEADING,
    MEDIA_FAILURE_NOTICE_HINT,
    RUNTIME_NOTICE_MARKER,
    STICKER_FAILURE_NOTICE_HEADING,
    clear_skill_roster_notice_stores,
    context_has_state,
    deliver_for_scope,
    drop_records_after,
    normalize_media_failures,
    notice_context_for,
    pending_media_failures,
    record_media_failures,
    strip_skill_roster_notice_echo,
)

SCOPE = "session:ext:qq-official-1906342047:98051d4c7475d34f"


def test_media_failure_notice_delivers_once_and_clears():
    clear_skill_roster_notice_stores()
    record_media_failures(
        SCOPE,
        [
            {"key": r"D:\NegiData\output\gone.png", "reason": "file not found"},
            {"key": r"D:\NegiData\secrets\x.png", "reason": "outside the allowed directories"},
        ],
    )
    rows = pending_media_failures(notice_context_for(SCOPE))
    assert [row["key"] for row in rows] == [
        r"D:\NegiData\output\gone.png",
        r"D:\NegiData\secrets\x.png",
    ]

    message, delivered = deliver_for_scope(SCOPE, [], now="2026-11-10T00:00:00+08:00")
    assert message is not None and delivered == []
    content = str(message["content"])
    assert content.startswith(RUNTIME_NOTICE_MARKER)
    assert MEDIA_FAILURE_NOTICE_HEADING in content
    assert r"- `D:\NegiData\output\gone.png` — file not found" in content
    assert r"- `D:\NegiData\secrets\x.png` — outside the allowed directories" in content
    assert MEDIA_FAILURE_NOTICE_HINT in content
    assert pending_media_failures(notice_context_for(SCOPE)) == []

    again, _delivered = deliver_for_scope(SCOPE, [], now="2026-11-10T00:01:00+08:00")
    assert again is None


def test_media_section_follows_the_sticker_section():
    clear_skill_roster_notice_stores()
    record_media_failures(SCOPE, [{"key": "D:/x/gone.png", "reason": "file not found"}])
    from g3ku.runtime.skill_roster_notice import record_sticker_failures

    record_sticker_failures(SCOPE, [{"key": "demo/shy", "reason": "分类不存在"}])

    message, _delivered = deliver_for_scope(SCOPE, [], now="2026-11-10T00:00:00+08:00")
    body = str(message["content"])[len(RUNTIME_NOTICE_MARKER):].lstrip()
    assert body.startswith(STICKER_FAILURE_NOTICE_HEADING)
    assert body.index(MEDIA_FAILURE_NOTICE_HEADING) > body.index(STICKER_FAILURE_NOTICE_HEADING)


def test_record_media_failures_merges_dedupes_and_caps():
    clear_skill_roster_notice_stores()
    record_media_failures(SCOPE, [{"key": "a.png", "reason": "file not found"}])
    record_media_failures(
        SCOPE,
        [
            {"key": "a.png", "reason": "duplicate spelling"},
            {"key": "b.png", "reason": "outside the allowed directories"},
        ],
    )
    rows = pending_media_failures(notice_context_for(SCOPE))
    assert rows == [
        {"key": "a.png", "reason": "file not found"},
        {"key": "b.png", "reason": "outside the allowed directories"},
    ]

    capped = normalize_media_failures(
        [{"key": f"f{index}.png", "reason": "file not found"} for index in range(20)]
    )
    assert len(capped) == 8
    assert normalize_media_failures([{"key": "", "reason": "x"}, "junk"]) == []


def test_truncate_clears_media_pending_and_state_detection_sees_it():
    clear_skill_roster_notice_stores()
    payload = {"media_failures_pending": [{"key": "a.png", "reason": "file not found"}]}
    assert context_has_state(payload) is True
    assert drop_records_after(payload, cutoff_iso="")["media_failures_pending"] == []
    assert (
        drop_records_after(payload, cutoff_iso="2026-11-10T00:00:00+08:00")[
            "media_failures_pending"
        ]
        == []
    )


def test_echo_strip_recognizes_media_section():
    notice = "\n".join(
        [RUNTIME_NOTICE_MARKER, MEDIA_FAILURE_NOTICE_HEADING, "- `a.png` — file not found"]
    )
    text = f"先回答用户的问题。\n\n{notice}"
    assert strip_skill_roster_notice_echo(text) == "先回答用户的问题。"


def test_relay_extraction_queues_unsent_media_for_the_session():
    from g3ku.runtime import external_events

    clear_skill_roster_notice_stores()
    outside = Path(__file__).resolve().parent / "does-not-exist.png"
    text, attachments = external_events._extract_media_attachments(
        f"![gone]({outside})", session_key="ext:qq-official-1906342047:98051d4c7475d34f"
    )

    assert attachments == []
    assert str(outside) in text
    assert pending_media_failures(notice_context_for(SCOPE)) == [
        {"key": str(outside), "reason": "file not found"}
    ]


def test_outbound_drain_extraction_queues_unsent_media_for_the_session():
    from g3ku.shells import web

    clear_skill_roster_notice_stores()
    outside = Path(__file__).resolve().parent / "does-not-exist.png"
    text, attachments = web._extract_outbound_media_attachments(
        f"![gone]({outside})", session_key="ext:qq-official-1906342047:98051d4c7475d34f"
    )

    assert attachments == []
    assert str(outside) in text
    assert pending_media_failures(notice_context_for(SCOPE)) == [
        {"key": str(outside), "reason": "file not found"}
    ]
