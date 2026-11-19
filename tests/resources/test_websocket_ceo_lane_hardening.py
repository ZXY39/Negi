"""CEO websocket 车道硬化：转发层不得把失败抛回回合，发送任务不得静默退出，暂停不得孤儿化排队条目。

三条都在 `ceo_websocket` 的闭包里，跑不起真 websocket，只能锁代码形状——但症状同属
"页面永久停在半截回合、刷新才同步"，值得钉住：

- `RuntimeAgentSession._emit` 逐个 await 订阅者且不捕获，所以转发层抛出的异常会打断
  正在收尾的回合（message_end 之后还有 state_snapshot/turn_end），前端就永远等不到收尾帧；
- `sender()` 若因单帧写失败而退出，socket 仍是开的，客户端收不到 close 也就不会自动重连；
- `client.pause_turn` 里的 `pause` 会经 `cancel_session_tasks` 掐掉本条回合链，链尾那次
  drain 因此不会再跑：暂停之后必须自己请一次接棒，否则点过插话的那条消息没人发。
"""

from __future__ import annotations

from pathlib import Path

from g3ku.runtime.api import websocket_ceo

SOURCE = Path(websocket_ceo.__file__).read_text(encoding="utf-8")


def _block(start_marker: str, end_marker: str) -> str:
    start = SOURCE.index(start_marker)
    return SOURCE[start:SOURCE.index(end_marker, start)]


def test_relay_guard_keeps_turn_emission_alive_on_forwarding_failure():
    guard = _block("async def relay_session_event(", "async def _relay_session_event(")
    assert "await _relay_session_event(event)" in guard, "转发主体未被包进守卫"
    assert "except Exception:" in guard and "logger.exception" in guard, "失败未记日志"
    assert "except asyncio.CancelledError" in guard, "取消必须继续上抛"
    # 订阅点必须挂守卫，而不是直接把裸转发函数交给 _emit。
    assert "session.subscribe(relay_session_event)" in SOURCE


def test_sender_drops_single_frame_failure_but_closes_a_dead_lane():
    body = _block("async def sender(", "async def relay_session_event(")
    assert "except WebSocketChannelClosed:\n                raise" in body, "客户端断开应照常终结该 sender"
    assert "failures >= _SENDER_CONSECUTIVE_FAILURE_LIMIT" in body, "缺少连续失败上限"
    assert "await websocket_close(websocket, code=1011)" in body, "放弃前必须关 socket 让前端重连"
    assert "closed.set()" in body
    assert "failures = 0" in body, "成功一帧后未清零"


def test_pause_hands_off_queued_follow_ups_instead_of_orphaning_them():
    branch = _block(
        "if message_type == 'client.pause_turn':",
        "if message_type != 'client.user_message':",
    )
    assert "await session.pause(manual=True)" in branch
    # manual_pause 是绕过手动暂停闸门的唯一凭据；换成别的来源名，那一条插话就留在队列里。
    assert "QUEUED_FOLLOW_UP_MANUAL_PAUSE_SOURCE" in branch, "暂停后必须按 manual_pause 来源请接棒"
    assert "request_queued_follow_up_dispatch" in branch, "暂停没有请求接棒"
    assert "has_queued_follow_up_messages" in branch, "队列空时不该起接棒任务"
