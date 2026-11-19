"""Windows IOCP 事件循环硬化（CPython gh-149123 形态）。

两层，均仅 Windows 生效、幂等：
1. poll 守卫：完成回调与"future 已被别的线程置终态"竞态时，_poll 抛出的
   InvalidStateError 不再炸掉整个事件循环。
2. 线程审计：_OverlappedFuture 的 cancel/set_result/set_exception 若发生在
   非 loop 线程，落 WARNING 带调用栈——直接点名肇事者，而不是靠猜。

开关：G3KU_IOCP_GUARD=0 同时关掉两层（A/B 对照用）。
"""

from __future__ import annotations

import asyncio
import functools
import os
import threading
import traceback

from loguru import logger

_AUDIT_LIMIT = 100
_audit_count = 0
_audit_seen: set[str] = set()


def _guard_enabled() -> bool:
    return os.name == "nt" and str(os.getenv("G3KU_IOCP_GUARD", "1")).strip() != "0"


def _make_guarded_poll(original_poll):
    @functools.wraps(original_poll)
    def guarded(self, timeout=None):
        try:
            return original_poll(self, timeout)
        except asyncio.InvalidStateError:
            # 竞态形态：future 在 getresult() 的 GIL 窗口里被别的线程置了终态。
            # 该 future 的等待方由置终态的一侧负责；循环本身继续转。
            logger.warning(
                "IOCP completion race survived: InvalidStateError in _poll "
                "(gh-149123 shape); event loop continues"
            )
            return None

    return guarded


def _record_cross_thread_mutation(method: str, owner_tid: int) -> None:
    global _audit_count
    if _audit_count >= _AUDIT_LIMIT:
        return
    stack = "".join(traceback.format_stack(limit=14))
    key = f"{method}:{hash(stack)}"
    if key in _audit_seen:
        return
    _audit_seen.add(key)
    _audit_count += 1
    logger.warning(
        "IOCP cross-thread future mutation: method={} loop_tid={} caller_tid={} caller={}\n{}",
        method,
        owner_tid,
        threading.get_ident(),
        threading.current_thread().name,
        stack,
    )


def _make_audited(method_name: str, original):
    @functools.wraps(original)
    def audited(self, *args, **kwargs):
        loop = self.get_loop()
        # _thread_id 由 run_forever 置位、退出清空；不在跑就无从判起，跳过。
        owner = getattr(loop, "_thread_id", None)
        if owner is not None and owner != threading.get_ident():
            _record_cross_thread_mutation(method_name, owner)
        return original(self, *args, **kwargs)

    return audited


def install_iocp_hardening() -> bool:
    if not _guard_enabled():
        return False
    from asyncio import windows_events

    proactor = windows_events.IocpProactor
    if not getattr(proactor, "_g3ku_poll_guarded", False):
        proactor._poll = _make_guarded_poll(proactor._poll)
        proactor._g3ku_poll_guarded = True

    future_cls = windows_events._OverlappedFuture
    if not getattr(future_cls, "_g3ku_thread_audited", False):
        for name in ("cancel", "set_result", "set_exception"):
            setattr(future_cls, name, _make_audited(name, getattr(future_cls, name)))
        future_cls._g3ku_thread_audited = True

    logger.info("IOCP hardening installed (poll guard + cross-thread future audit)")
    return True
