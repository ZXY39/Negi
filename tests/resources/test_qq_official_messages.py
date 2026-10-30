"""Unit tests for the pure QQ-official mapping helpers (no botpy, no I/O)."""

from g3ku.qq_official.messages import (
    external_key_for_c2c,
    external_key_for_group,
    external_key_for_guild,
    external_key_for_guild_dm,
    idempotency_key_for,
    is_deliverable_event,
    parse_external_key,
    render_qq_markdown,
)


def test_render_qq_markdown_only_adds_blank_line_before_ordered_lists() -> None:
    """编号列表前补空行是实测出来的唯一一条归一；别的形状一律不动。"""
    assert render_qq_markdown("结论如下：\n1. 先做这个\n2. 再做这个") == (
        "结论如下：\n\n1. 先做这个\n2. 再做这个"
    )
    # 列表自己连续的行不补；开头就是列表也没有前一行可补
    assert render_qq_markdown("1. 甲\n2. 乙") == "1. 甲\n2. 乙"
    # 无序列表实测直接成行，不补
    assert render_qq_markdown("正文\n- 甲\n- 乙") == "正文\n- 甲\n- 乙"
    # 围栏内的行是用户的代码，原样保留
    assert render_qq_markdown("步骤：\n```text\nfoo\n1. bar\n```") == "步骤：\n```text\nfoo\n1. bar\n```"


def test_external_key_builders() -> None:
    assert external_key_for_group("g1") == "qq:group:g1"
    assert external_key_for_c2c("u1") == "qq:c2c:u1"
    assert external_key_for_guild("G", "C") == "qq:guild:G:C"
    assert external_key_for_guild_dm("G", "U") == "qq:guilddm:G:U"


def test_parse_external_key_roundtrip() -> None:
    assert parse_external_key("qq:group:g1") == ("group", {"group_openid": "g1"})
    assert parse_external_key("qq:c2c:u1") == ("c2c", {"user_openid": "u1"})
    assert parse_external_key("qq:guild:G:C") == ("guild", {"guild_id": "G", "channel_id": "C"})
    assert parse_external_key("qq:guilddm:G:U") == ("guilddm", {"guild_id": "G", "user_id": "U"})


def test_parse_external_key_unknown_target() -> None:
    assert parse_external_key("china:qqbot:default:dm") == ("unknown", {})
    assert parse_external_key("") == ("unknown", {})


def test_idempotency_and_deliverable() -> None:
    assert idempotency_key_for("123") == "qq-123"
    assert idempotency_key_for("") == ""
    assert is_deliverable_event("reply.final") is True
    assert is_deliverable_event("outbound.created") is True
    assert is_deliverable_event("reply.delta") is False
    assert is_deliverable_event("progress") is False
