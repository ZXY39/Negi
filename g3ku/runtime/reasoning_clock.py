"""思考秒表：前门回合与节点轮次共用的唯一计时口径。

口径：一段 = 该跳请求起点 → 最后一个思维块到达。思维吐完之后模型还在写工具调用参数
或正文，那一段不算思考（实测一跳读数 1.200s 里真思考只有 0.438s）；一个思维块都没到
的跳贡献 0。收口点由调用方决定：该跳模型流返回时收一次，回合/轮次落账时再收一次。

三个读数都是公开字段而不是私有状态：测窗口时要按"这段已经走了多久"改起点，
两条车道的计时合同测试都从那里喂时间。
"""

from __future__ import annotations

import asyncio
from typing import Optional


def monotonic_now() -> float:
    """与两条车道一致的时间源：有事件循环就取循环时钟。"""
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        return 0.0


class ReasoningSecondsClock:
    """累计「已收口各段 + 正在发的这一段」的思考秒数。"""

    def __init__(self) -> None:
        self.closed_seconds: float = 0.0
        self.segment_started: float = 0.0
        self.segment_last_chunk: float = 0.0

    def begin_segment(self, now: Optional[float] = None) -> None:
        # 先收上一发再开这一段：同跳重发与换链降级都会重放整条思考，不收口就把两发的
        # 窗口接成一段。
        self.close_segment()
        self.segment_started = monotonic_now() if now is None else float(now or 0.0)
        self.segment_last_chunk = 0.0

    def note_chunk(self, now: Optional[float] = None) -> None:
        """每到一个思维块把终点往前推一格；之后写工具参数或正文都不再推。"""
        stamp = monotonic_now() if now is None else float(now or 0.0)
        if stamp <= 0.0:
            return
        self.segment_last_chunk = stamp

    def close_segment(self) -> bool:
        """收口正在发的这一段，返回是否真有一段可收。"""
        if self.segment_started <= 0.0:
            return False
        self.closed_seconds += self.segment_seconds()
        self.segment_started = 0.0
        self.segment_last_chunk = 0.0
        return True

    def segment_seconds(self) -> float:
        if self.segment_started <= 0.0 or self.segment_last_chunk <= 0.0:
            return 0.0
        return max(0.0, self.segment_last_chunk - self.segment_started)

    def total_seconds(self) -> float:
        return float(self.closed_seconds or 0.0) + self.segment_seconds()

    def reset_totals(self) -> None:
        """只清累计：划回合/轮次边界用，正在发的这一段留着。"""
        self.closed_seconds = 0.0

    def reset_segment(self) -> None:
        """只清这一段：换发但不丢已累计读数时用。"""
        self.segment_started = 0.0
        self.segment_last_chunk = 0.0
