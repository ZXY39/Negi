"""提示词注入侧的统一本地时间渲染。

模型上下文原本没有任何"现在几点"的信息：system prompt 不含时间，历史记录的
timestamp 字段也不会被投影进请求（`frontdoor/message_builder._history_message`
只取 role/content/metadata）。cron 事件只带裸毫秒时间戳，模型只能心算换算或
从上下文残片猜测，实际发生过时区换算错误（把 23:00 GMT+8 的准点投递算成
"延迟 8 小时"）与凭空引用陈旧时间两类事故。

因此所有注入给模型的消息（普通用户消息、heartbeat 事件束、cron 事件）统一
携带已换算好的本地墙钟时间（带 UTC 偏移与星期），让模型被唤醒时立即获得
时间锚点，不再需要自行换算毫秒时间戳。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

_WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 用户消息投影装饰的标记。持久化转录保持用户原文（RAG ingest、web UI 等
# 消费方依赖 raw content），时间戳只在构造模型请求时由投影层追加；所有基于
# 内容相等性的比较（_cron_prompt_equal、暂停回合对账等）必须先用
# strip_arrival_time_stamp 还原原文。与 cron_hidden_prompt 的"宿主追加文本 +
# 比较时剥离"是同一模式。
MESSAGE_ARRIVAL_TIME_MARKER = "[消息送达时间]"


def render_local_time(value: datetime | None = None) -> str:
    """渲染为 ``2026-09-05 10:38:39 +08:00（周六）`` 格式的本地时间文本。

    默认取当前本地时间。naive datetime 按本地时区补齐偏移，与运行时其余
    ``datetime.now()`` 语义保持一致。
    """
    moment = value if isinstance(value, datetime) else datetime.now()
    if moment.tzinfo is None:
        moment = moment.astimezone()
    offset = moment.strftime("%z")
    offset_text = f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset
    return f"{moment.strftime('%Y-%m-%d %H:%M:%S')} {offset_text}（{_WEEKDAY_CN[moment.weekday()]}）"


def render_epoch_ms_local(value: Any) -> str:
    """把 epoch 毫秒时间戳换算成本地墙钟时间文本；无效或非正值返回空串。

    用于 cron 事件里的 ``scheduled_run_at_ms`` / ``last_delivered_at_ms``：
    原始毫秒字段保留（契约兼容），旁边补充换算好的可读时间，模型不再心算。
    """
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    try:
        return render_local_time(datetime.fromtimestamp(ms / 1000).astimezone())
    except (OverflowError, OSError, ValueError):
        return ""


def render_arrival_stamp(value: Any) -> str:
    """把转录记录的 timestamp（ISO 字符串或 datetime）渲染为送达时间戳文本。

    返回形如 ``\\n\\n[消息送达时间] 2026-09-05 10:38:39 +08:00（周六）`` 的追加段；
    空值或无法解析时返回空串（绝不臆造时间，宁缺毋假）。
    """
    moment: datetime | None = None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if moment is None:
        return ""
    return f"\n\n{MESSAGE_ARRIVAL_TIME_MARKER} {render_local_time(moment)}"


def strip_arrival_time_stamp(text: Any) -> str:
    """剥离尾部的送达时间戳行，还原用户原文，供内容相等性比较使用。

    只处理"最后一行以标记开头"的情况：时间戳永远是投影层追加的末尾行，
    用户正文里恰好出现标记文本时不会被误删。
    """
    raw = str(text or "")
    lines = raw.split("\n")
    if lines and lines[-1].strip().startswith(MESSAGE_ARRIVAL_TIME_MARKER):
        return "\n".join(lines[:-1]).rstrip()
    return raw


__all__ = [
    "MESSAGE_ARRIVAL_TIME_MARKER",
    "render_arrival_stamp",
    "render_epoch_ms_local",
    "render_local_time",
    "strip_arrival_time_stamp",
]
