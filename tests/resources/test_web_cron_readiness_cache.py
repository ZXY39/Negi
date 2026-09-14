"""web cron 就绪判定与端口归属缓存的回归测试。

根因背景：`_cron_runtime_ready` 曾先跑 `_should_start_web_cron`（同步起
netstat/ss 子进程探测端口归属）再查 cron 状态。worker 高频回调都走
`ensure_web_runtime_services`，连接数多的机器上每次探测可达秒级，整个
事件循环被反复冻结（任务大厅请求批量挂起）。修复后：cron 已运行时直接
就绪不探测；归属探测结果缓存，确认归属后永久生效、失败/未归属按短 TTL 重试。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from g3ku.shells import web as web_shell


class _Heartbeat:
    def __init__(self) -> None:
        self._started = True


class _MainService:
    def __init__(self) -> None:
        self._started = True

    async def startup(self) -> None:
        self._started = True


class _CronService:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.start_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.enabled = True

    def status(self) -> dict[str, object]:
        return {"enabled": self.enabled}


@pytest.fixture(autouse=True)
def _reset_port_cache(monkeypatch):
    monkeypatch.setattr(web_shell, "_PORT_OWNERSHIP_CACHE", {})
    yield


def test_cron_runtime_ready_skips_port_probe_when_running(monkeypatch) -> None:
    probe_calls: list[int] = []

    def _probe(port):
        probe_calls.append(port)
        return {os.getpid()}

    monkeypatch.setattr(web_shell, "_listen_port_owners", _probe)
    agent = SimpleNamespace(cron_service=_CronService(enabled=True))

    assert web_shell._cron_runtime_ready(agent) is True
    assert probe_calls == []


def test_cron_runtime_ready_not_running_checks_ownership(monkeypatch) -> None:
    probe_calls: list[int] = []

    def _probe(port):
        probe_calls.append(port)
        return set()  # 端口归别的进程

    monkeypatch.setattr(web_shell, "_listen_port_owners", _probe)
    agent = SimpleNamespace(cron_service=_CronService(enabled=False))

    # 不是本进程的端口 → 启动 cron 不归本进程管 → 视为就绪
    assert web_shell._cron_runtime_ready(agent) is True

    monkeypatch.setattr(web_shell, "_PORT_OWNERSHIP_CACHE", {})
    monkeypatch.setattr(web_shell, "_listen_port_owners", lambda port: {os.getpid()})
    # 本进程持有端口但 cron 未运行 → 需要启动 → 未就绪
    assert web_shell._cron_runtime_ready(agent) is False


def test_process_owns_listen_port_caches_positive_forever(monkeypatch) -> None:
    calls: list[int] = []

    def _probe(port):
        calls.append(port)
        return {os.getpid()}

    monkeypatch.setattr(web_shell, "_listen_port_owners", _probe)

    first = web_shell._process_owns_listen_port(18790)
    second = web_shell._process_owns_listen_port(18790)

    assert first is True
    assert second is True
    assert len(calls) == 1  # 确认归属后不再起子进程


def test_process_owns_listen_port_retries_negative_after_ttl(monkeypatch) -> None:
    calls: list[int] = []

    def _probe(port):
        calls.append(port)
        return set()

    monkeypatch.setattr(web_shell, "_listen_port_owners", _probe)

    assert web_shell._process_owns_listen_port(18790) is False
    assert web_shell._process_owns_listen_port(18790) is False
    assert len(calls) == 1  # TTL 内命中缓存

    monkeypatch.setattr(web_shell, "_PORT_OWNERSHIP_RETRY_AFTER_S", 0.0)
    assert web_shell._process_owns_listen_port(18790) is False
    assert len(calls) == 2  # TTL 过期后重新探测


@pytest.mark.asyncio
async def test_ensure_web_runtime_services_fast_path_never_probes(monkeypatch) -> None:
    """一切就绪时高频回调不得触发端口探测（事件循环保护）。"""
    probe_calls: list[int] = []

    def _probe(port):
        probe_calls.append(port)
        return {os.getpid()}

    monkeypatch.setattr(web_shell, "_listen_port_owners", _probe)
    monkeypatch.setattr(web_shell, "_global_runtime_services_lock", None)
    monkeypatch.setattr(web_shell, "_global_web_heartbeat", _Heartbeat())

    agent = SimpleNamespace(
        main_task_service=_MainService(),
        cron_service=_CronService(enabled=True),
    )

    # 模拟 worker 事件回调的高频调用
    for _ in range(5):
        await web_shell.ensure_web_runtime_services(agent)

    assert probe_calls == []


@pytest.mark.asyncio
async def test_ensure_web_runtime_services_starts_cron_for_owner_once(monkeypatch) -> None:
    """本进程持有端口且 cron 未运行：启动一次，随后调用走缓存不再探测。"""
    probe_calls: list[int] = []

    def _probe(port):
        probe_calls.append(port)
        return {os.getpid()}

    async def _start_heartbeat(_agent, _runtime_manager, **kwargs):
        _ = _agent, _runtime_manager, kwargs
        return _Heartbeat()

    async def _ensure_worker(_service, *, wait_timeout_s: float = 5.0):
        _ = _service, wait_timeout_s
        return False

    monkeypatch.setattr(web_shell, "_listen_port_owners", _probe)
    monkeypatch.setattr(web_shell, "_global_runtime_services_lock", None)
    monkeypatch.setattr(web_shell, "_global_web_heartbeat", None)
    monkeypatch.setattr(web_shell, "ensure_managed_task_worker", _ensure_worker)
    monkeypatch.setattr(web_shell, "get_runtime_manager", lambda _agent=None: object())
    monkeypatch.setattr(web_shell, "start_web_session_heartbeat", _start_heartbeat)

    cron_service = _CronService(enabled=False)
    agent = SimpleNamespace(main_task_service=_MainService(), cron_service=cron_service)

    await web_shell.ensure_web_runtime_services(agent)
    assert cron_service.start_calls == 1

    await web_shell.ensure_web_runtime_services(agent)
    assert cron_service.start_calls == 1
    # 首次探测一次（结果缓存），之后的就绪判定不再起子进程
    assert len(probe_calls) == 1
