"""web cron 的归属判据：单实例锁，不是监听面。

根因背景：这条判定曾起 netstat/ss 子进程问"谁在监听 web 端口"。端口要等 uvicorn
走完 lifespan 才 bind，而判定跑在启动体里——冷启动时监听面是空的，空集被读成
"端口归别人"，cron 被静默跳过；同一条探测还挂在高频回调上，连接数多的机器上每次
可达秒级。现在读 ``.g3ku/start.lock`` 的进程内句柄：它在 bind 之前就已定，且免费。
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import g3ku.shells.web as web_shell
import g3ku.web.launcher as launcher


class _CronService:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.enabled = True

    def status(self) -> dict[str, object]:
        return {"enabled": self.enabled}


def _hold_web_start_lock(monkeypatch, held: bool) -> None:
    monkeypatch.setattr(launcher, "_START_LOCK_HANDLE", object() if held else None)


def test_cron_attribution_follows_start_lock(monkeypatch) -> None:
    _hold_web_start_lock(monkeypatch, True)
    assert launcher.holds_web_start_lock() is True
    assert web_shell._should_start_web_cron() is True

    _hold_web_start_lock(monkeypatch, False)
    assert launcher.holds_web_start_lock() is False
    assert web_shell._should_start_web_cron() is False


def test_cron_attribution_never_probes_the_listening_socket(monkeypatch) -> None:
    """冷启动时端口还不存在，判定也就不许去问它。"""
    probes: list[str] = []

    def _no_subprocess(*args, **kwargs):
        _ = args, kwargs
        probes.append("probe")
        raise AssertionError("cron attribution must not shell out to a port probe")

    monkeypatch.setattr(subprocess, "run", _no_subprocess)
    _hold_web_start_lock(monkeypatch, True)

    assert web_shell._should_start_web_cron() is True
    assert probes == []


@pytest.mark.parametrize("held", [True, False])
def test_cron_not_owned_by_this_process_counts_as_ready(monkeypatch, held: bool) -> None:
    """`_cron_runtime_ready` 的两档：在跑=就绪；该我起而没起=未就绪；不归我起=就绪。

    「不归我起」必须读成就绪，否则每个调用者都会为了起 cron 反复重跑整个启动体。
    """
    _hold_web_start_lock(monkeypatch, held)
    running = SimpleNamespace(cron_service=_CronService(enabled=True))
    assert web_shell._cron_runtime_ready(running) is True

    idle = SimpleNamespace(cron_service=_CronService(enabled=False))
    assert web_shell._cron_runtime_ready(idle) is (not held)
