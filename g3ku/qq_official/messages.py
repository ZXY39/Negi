"""Pure mapping helpers for the QQ official adapter.

No botpy import, no I/O: everything here translates between QQ-side
identifiers and the External Agent API's ``external_key`` namespace, so it is
fully unit-testable.

external_key shapes (bridge-owned, opaque to g3ku):
- 群 @机器人 / 群消息: ``qq:group:<group_openid>``
- 单聊(好友):          ``qq:c2c:<user_openid>``
- 频道:                ``qq:guild:<guild_id>:<channel_id>``
- 频道私信:            ``qq:guilddm:<guild_id>:<user_openid>``
"""

from __future__ import annotations

import re
from typing import Any

QQ_BRIDGE_ID = "qq-official"

_ORDERED_LIST_ITEM_RE = re.compile(r"^[ \t]*\d+[.)][ \t]+\S")
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~)")


def render_qq_markdown(text: str) -> str:
    """把回复正文收成 QQ 能按 markdown 渲染的形状。

    实测定的一条：编号列表项紧跟在非空行之后（只隔一个换行）时，QQ 客户端丢掉编号、
    把它画成上一项的嵌套子项，并且把紧邻的那行正文一起吸进列表层级；前面补一个空行
    就正常。其余语法（标题、粗斜体、删除线、链接、列表、引用、代码块、行内代码、
    表格、分割线）真机全部渲染，单换行也直接成行，因此这里不做任何别的改写。

    围栏内的行原样保留：代码块在 QQ 里是按代码块画的，往里面插空就是改用户的代码。
    """
    out: list[str] = []
    in_fence = False
    for line in str(text or "").split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
        elif (
            not in_fence
            and out
            and out[-1].strip()
            and _ORDERED_LIST_ITEM_RE.match(line)
            and not _ORDERED_LIST_ITEM_RE.match(out[-1])
        ):
            out.append("")
        out.append(line)
    return "\n".join(out)



def bridge_id_for_app_id(app_id: str) -> str:
    """一个 AppID = 一个桥身份（``qq-official-<appId>``）。

    身份决定三件事：用哪条 ``externalApi.tokens`` 凭证、会话键的 bridge 段、以及
    registry/outbox 的可见集作用域——所以多号之间的滞留推送按构造互不可见。
    调用方需保证 ``app_id`` 非空且已由配置层 ``_normalize_qq_app_id`` 收过字符集
    （不含 ``.``，密钥覆盖层按点分路径寻址才不歧义）。
    """
    return f"{QQ_BRIDGE_ID}-{str(app_id or '').strip()}"


def external_key_for_group(group_openid: Any) -> str:
    return f"qq:group:{str(group_openid or '').strip()}"


def external_key_for_c2c(user_openid: Any) -> str:
    return f"qq:c2c:{str(user_openid or '').strip()}"


def external_key_for_guild(guild_id: Any, channel_id: Any) -> str:
    return f"qq:guild:{str(guild_id or '').strip()}:{str(channel_id or '').strip()}"


def external_key_for_guild_dm(guild_id: Any, user_id: Any) -> str:
    return f"qq:guilddm:{str(guild_id or '').strip()}:{str(user_id or '').strip()}"


def idempotency_key_for(event_id: Any) -> str:
    raw = str(event_id or "").strip()
    return f"qq-{raw}" if raw else ""


def parse_external_key(external_key: str) -> tuple[str, dict[str, str]]:
    """Inverse mapping: external_key -> (kind, target) for reply delivery.

    kind is one of ``group`` / ``c2c`` / ``guild`` / ``guilddm`` / ``unknown``
    and target carries the fields the botpy post_* call needs.
    """
    parts = str(external_key or "").strip().split(":")
    if len(parts) >= 3 and parts[0] == "qq":
        kind = parts[1]
        if kind == "group" and len(parts) == 3:
            return "group", {"group_openid": parts[2]}
        if kind == "c2c" and len(parts) == 3:
            return "c2c", {"user_openid": parts[2]}
        if kind == "guild" and len(parts) == 4:
            return "guild", {"guild_id": parts[2], "channel_id": parts[3]}
        if kind == "guilddm" and len(parts) == 4:
            return "guilddm", {"guild_id": parts[2], "user_id": parts[3]}
    return "unknown", {}


# Event types the bridge consumes from ``/api/v1/sessions/{id}/events``.
REPLY_FINAL_EVENT = "reply.final"
REPLY_DELTA_EVENT = "reply.delta"
OUTBOUND_EVENT = "outbound.created"


def is_deliverable_event(event_type: str) -> bool:
    return event_type in (REPLY_FINAL_EVENT, OUTBOUND_EVENT)
