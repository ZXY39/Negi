"""时钟读数与唤醒时刻的解析：两条车道共用一份，回执才可能逐字节同形。

放在这里而不是 `main/runtime/` 是因为前门（`g3ku/runtime/frontdoor/*`）与节点
（`main/runtime/*`）都要读它，而 `g3ku/runtime/tool_visibility` 那条 `base → …` 的循环
限制同样适用于这里：本模块只依赖标准库。

两条判据写在这里而不是各自车道里：

* **naive 时间按运行时本地时区解读**。`main.protocol.now_iso` 印的就是带本地偏移的 ISO 串，
  模型读它、再把它抄回 `at`，两侧必须用同一个偏移，否则"10:00 唤醒"会偏好几个小时。
* **形状宽容，判定严格**。模型可能交回 ISO、日期+时间、空格分隔、epoch 秒或 epoch 毫秒
  （`time` 一次就把这几种都给了它）。读侧全部归一；归一不了才打回，并且打回正文里带上
  当前时间——只说"格式不对"会让模型再猜一轮。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

WAKE_HORIZON_SECONDS = 30 * 24 * 60 * 60
# 会话车道的一次性唤醒在 cron 存储里的身份标记。名字前缀是它唯一的可寻址形状：
# "重新设就是覆盖"要靠它找到上一条并先删掉，而不是再排一份；操作员在 cron 列表里
# 也按这个前缀认出"这是模型自己设的闹钟"，不是给用户的提醒。
WAKE_JOB_NAME_PREFIX = 'self-wake: '
# 到点投递给模型的那句话的开头。cron 车道不会给内部回合加任何标签，所以"这是你
# 自己早先设的闹钟"必须由正文自己说清楚，否则模型会把它读成用户发来的新要求。
WAKE_MESSAGE_HEAD = 'Self wake-up you armed earlier:'
_WEEKDAY_NAMES = (
    'Monday',
    'Tuesday',
    'Wednesday',
    'Thursday',
    'Friday',
    'Saturday',
    'Sunday',
)


def now_ms() -> int:
    return int(datetime.now().astimezone().timestamp() * 1000)


def iso_from_ms(epoch_ms: int) -> str:
    return (
        datetime.fromtimestamp(int(epoch_ms) / 1000).astimezone().isoformat(timespec='seconds')
    )


def clock_payload(*, epoch_ms: int | None = None) -> dict[str, Any]:
    """一份权威时钟读数：本地 ISO、UTC、epoch 毫秒、星期、偏移。

    星期是显式算的而不是 `strftime('%A')`：后者跟着 locale 出中文，而模型可见文案是英文。
    """
    moment = int(epoch_ms) if epoch_ms is not None else now_ms()
    local = datetime.fromtimestamp(moment / 1000).astimezone()
    utc = local.astimezone(timezone.utc)
    return {
        'now': local.isoformat(timespec='seconds'),
        'utc': utc.isoformat(timespec='seconds'),
        'epoch_ms': moment,
        'weekday': _WEEKDAY_NAMES[local.weekday()],
        'utc_offset': local.strftime('%z')[:3] + ':' + local.strftime('%z')[3:],
    }


def resolve_wake_moment(
    *,
    at: Any = None,
    after_seconds: Any = None,
    current_ms: int | None = None,
) -> tuple[int, str]:
    """把 `at` / `after_seconds` 归一成 ``(wake_epoch_ms, error)``。

    两个都给或都不给、时间在过去、超出 `WAKE_HORIZON_SECONDS` 都算 error；error 正文里带上
    当前时间，模型这一跳就能直接改对。
    """
    current = int(current_ms) if current_ms is not None else now_ms()
    text = str(at or '').strip()
    delay = after_seconds
    has_delay = delay not in (None, '')
    if text and has_delay:
        return 0, f'Give either `at` or `after_seconds`, not both. Current time: {iso_from_ms(current)}'
    if not text and not has_delay:
        return 0, f'Give `after_seconds` or an absolute `at`. Current time: {iso_from_ms(current)}'

    if has_delay:
        try:
            seconds = int(float(str(delay).strip()))
        except (TypeError, ValueError):
            return 0, f'`after_seconds` must be a whole number of seconds. Current time: {iso_from_ms(current)}'
        if seconds < 1:
            return 0, f'`after_seconds` must be at least 1. Current time: {iso_from_ms(current)}'
        wake = current + seconds * 1000
    else:
        parsed = _parse_moment(text)
        if parsed is None:
            return 0, (
                f'`at` is not a time I can read: {text!r}. Pass the ISO text from the time tool '
                f'(e.g. "2026-10-26T10:33:00+08:00") or use `after_seconds`. '
                f'Current time: {iso_from_ms(current)}'
            )
        wake = parsed
        if wake <= current:
            return 0, (
                f'`at` is already in the past ({iso_from_ms(wake)}). Do it now instead of scheduling '
                f'it, or pick a later moment. Current time: {iso_from_ms(current)}'
            )

    if wake - current > WAKE_HORIZON_SECONDS * 1000:
        return 0, (
            f'`{ "at" if not has_delay else "after_seconds" }` is more than '
            f'{WAKE_HORIZON_SECONDS // 86400} days out. Current time: {iso_from_ms(current)}'
        )
    return wake, ''


def _parse_moment(text: str) -> int | None:
    """ISO / 日期时间 / 纯数字 epoch（秒或毫秒）都收；解析不了回 None。"""
    cleaned = text.strip().replace('/', '-').replace('T', ' ').replace('Z', '+00:00')
    if cleaned.lstrip('-').isdigit():
        digits = int(cleaned)
        # 10 位按秒、13 位按毫秒；其余长度交给 ISO 分支去试，别把 "2026" 这类读成时刻。
        if len(str(digits)) == 10:
            return digits * 1000
        if len(str(digits)) == 13:
            return digits
        return None
    for candidate in (text.strip(), cleaned):
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            # naive = 运行时本地时区，与 `now_iso` 同一偏移。
            parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return int(parsed.timestamp() * 1000)
    try:
        parsed = datetime.strptime(cleaned, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None
    return int(parsed.replace(tzinfo=datetime.now().astimezone().tzinfo).timestamp() * 1000)


def describe_delay(*, wake_at_ms: int, current_ms: int) -> str:
    """把"还有多久到点"写成模型能直接读的一段，不在文案里做时区换算。"""
    return f'{max(0, int(wake_at_ms) - int(current_ms)) // 1000}s from now'


__all__ = [
    'WAKE_HORIZON_SECONDS',
    'clock_payload',
    'describe_delay',
    'iso_from_ms',
    'now_ms',
    'resolve_wake_moment',
]
