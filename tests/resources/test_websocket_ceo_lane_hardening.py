"""CEO websocket 车道硬化：转发层不得把失败抛回回合，发送任务不得静默退出。

两条都在 `ceo_websocket` 的闭包里，跑不起真 websocket，只能锁代码形状——但症状同属
"页面永久停在半截回合、刷新才同步"，值得钉住：

- `RuntimeAgentSession._emit` 逐个 await 订阅者且不捕获，所以转发层抛出的异常会打断
  正在收尾的回合（message_end 之后还有 state_snapshot/turn_end），前端就永远等不到收尾帧；
- `sender()` 若因单帧写失败而退出，socket 仍是开的，客户端收不到 close 也就不会自动重连。
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
