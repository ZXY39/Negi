from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace

import httpx
import pytest

from main.service.runtime_service import (
    _CALLBACK_CONNECT_TIMEOUT_SECONDS,
    _CALLBACK_POOL_KEEPALIVE_EXPIRY_SECONDS,
    _CALLBACK_POOL_MAX_CONNECTIONS,
    _CALLBACK_POOL_MAX_KEEPALIVE_CONNECTIONS,
    MainRuntimeService,
)

# uvicorn `timeout_keep_alive` 的默认值：客户端的 keep-alive 必须比它先到期，
# 否则"谁先关空闲连接"变成竞态，池里会留下一条已被对端 FIN 掉的"可用"连接。
UVICORN_KEEP_ALIVE_SECONDS = 5.0


def test_callback_pool_expires_before_the_server_closes_idle_sockets() -> None:
    limits = MainRuntimeService._callback_client_limits()

    assert limits.max_connections == _CALLBACK_POOL_MAX_CONNECTIONS
    assert limits.max_keepalive_connections == _CALLBACK_POOL_MAX_KEEPALIVE_CONNECTIONS
    assert limits.keepalive_expiry == _CALLBACK_POOL_KEEPALIVE_EXPIRY_SECONDS
    assert limits.keepalive_expiry < UVICORN_KEEP_ALIVE_SECONDS


def test_callback_client_is_built_once_with_those_limits(monkeypatch) -> None:
    import main.service.runtime_service as module

    calls: list[dict] = []

    def _factory(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(name='client')

    monkeypatch.setattr(module.httpx, 'AsyncClient', _factory)
    service = SimpleNamespace(_callback_client=None, _callback_client_limits=MainRuntimeService._callback_client_limits)

    first = MainRuntimeService._get_callback_client(service)
    second = MainRuntimeService._get_callback_client(service)

    assert first is second
    assert len(calls) == 1
    assert isinstance(calls[0]['limits'], httpx.Limits)


class _RaisingClient:
    def __init__(self, error: Exception) -> None:
        self._error = error
        self.posts = 0
        self.timeouts: list[object] = []

    async def post(self, *args, **kwargs):
        self.posts += 1
        self.timeouts.append(kwargs.get('timeout'))
        raise self._error


class _Stub:
    """只借真方法的壳：回调车道的计数与抛错必须按真实调用形状验，不能只测纯函数。"""

    _post_internal_callback = MainRuntimeService._post_internal_callback
    _note_callback_delivery_error = MainRuntimeService._note_callback_delivery_error
    _callback_pool_snapshot = MainRuntimeService._callback_pool_snapshot

    def __init__(self, client: _RaisingClient) -> None:
        self._callback_client = client
        self._callback_delivery_errors: dict[str, int] = {}
        self._callback_error_logged_mono = 0.0
        self._task_event_stats = {'callback_delivery_error_count': 0.0}

    def _get_callback_client(self) -> _RaisingClient:
        return self._callback_client


async def _post(service: _Stub) -> None:
    await service._post_internal_callback(
        'http://127.0.0.1:18790/api/internal/task-event',
        payload={'event_type': 'task.live.patch'},
        headers={},
        timeout=1.5,
    )


def test_a_failed_delivery_is_counted_by_type_and_re_raised() -> None:
    """这条车道的失败原来只落在 logger.debug，实盘按 "delivery skipped" 搜整窗口得 0 命中。
    计数是唯一能证明它发生过多少次的东西，且不许改变调用方看到的异常。"""
    client = _RaisingClient(httpx.ReadTimeout('slow'))
    service = _Stub(client)

    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(_post(service))

    assert service._callback_delivery_errors == {'ReadTimeout': 1}
    assert service._task_event_stats['callback_delivery_error_count'] == 1.0


def test_cancellation_is_not_counted_as_a_delivery_error() -> None:
    client = _RaisingClient(asyncio.CancelledError())
    service = _Stub(client)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_post(service))

    assert service._callback_delivery_errors == {}
    assert service._task_event_stats['callback_delivery_error_count'] == 0.0


def test_error_note_is_rate_limited_but_always_counted(monkeypatch) -> None:
    import main.service.runtime_service as module

    logged: list[str] = []

    class _Recorder:
        def warning(self, message: str, *args) -> None:
            logged.append(str(message).format(*args) if args else str(message))

    monkeypatch.setattr(module, 'logger', _Recorder())
    service = _Stub(_RaisingClient(httpx.ReadTimeout('slow')))

    MainRuntimeService._note_callback_delivery_error(service, 'ReadTimeout')
    MainRuntimeService._note_callback_delivery_error(service, 'ReadTimeout')
    MainRuntimeService._note_callback_delivery_error(service, 'ConnectError')

    assert service._callback_delivery_errors == {'ReadTimeout': 2, 'ConnectError': 1}
    assert service._task_event_stats['callback_delivery_error_count'] == 3.0
    assert len(logged) == 1
    assert "by_type={'ReadTimeout': 1}" in logged[0]


def test_connect_gets_its_own_longer_budget() -> None:
    """漏 socket 的那一下发生在 connect：握手必须比读/写有更大的余量。

    单条 1.5 s、批量 2.0 s 的整包超时把四档一起压死，web 侧接受稍慢就先判 connect
    超时，而那次取消会留下一条没人认领的 ESTABLISHED（census 标签
    `method<_loop_reading>`）。read/write 仍按调用方给的值，不许被顺带放宽。
    """
    client = _RaisingClient(httpx.ConnectTimeout('slow accept'))
    service = _Stub(client)

    with pytest.raises(httpx.ConnectTimeout):
        asyncio.run(_post(service))

    timeout = client.timeouts[0]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect >= _CALLBACK_CONNECT_TIMEOUT_SECONDS
    assert timeout.read == 1.5
    assert timeout.write == 1.5


def test_referrer_labels_name_the_pending_proactor_operation() -> None:
    """孤儿的 transport 只被一个挂起的 proactor 操作引用；recv / send / connect 三种
    形状修法完全不同，所以标签里必须带上方法名。"""

    class _Holder:
        def recv_into(self, *args):
            return None

    label = MainRuntimeService._census_referrer_label(MethodType(_Holder.recv_into, _Holder()))

    assert label == 'method<recv_into>'
