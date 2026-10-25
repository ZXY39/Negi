from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace

import httpx
import pytest

from main.service.runtime_service import (
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
    _deliver_task_event_callback = MainRuntimeService._deliver_task_event_callback
    _note_task_event_delivery_drop = MainRuntimeService._note_task_event_delivery_drop

    def __init__(self, client: _RaisingClient) -> None:
        self._callback_client = client
        self._callback_delivery_errors: dict[str, int] = {}
        self._callback_error_logged_mono = 0.0
        self._task_event_stats = {
            'callback_delivery_error_count': 0.0,
            'task_event_delivery_dropped_count': 0.0,
        }
        self._task_event_delivery_dropped_by_type: dict[str, int] = {}
        self._task_event_delivery_logged_mono = 0.0
        self._internal_callback_targets = lambda **_kwargs: [
            ('http://127.0.0.1:18790/api/internal/task-event', 'token')
        ]
        self._callback_headers = lambda **_kwargs: {}

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


def test_connect_gets_no_special_budget() -> None:
    """漏 socket 的那一下发生在 connect：握手必须比读/写有更大的余量。

    单条 1.5 s、批量 2.0 s 的整包超时把四档一起压死，`db724884` 因此给 connect 抬到 5 s。
    上线后计数当场把它否掉：ConnectTimeout 仍 35 次/7 分钟（≈5 次/分），与抬档前同一量级
    ——判超时的和被判的是同一个卡住的循环，放宽只是换个更晚的时刻输，还让排空口多等 3.5 s。
    四档继续一起用调用方给的值，不许再"顺手"放宽 connect。
    """
    client = _RaisingClient(httpx.ConnectTimeout('slow accept'))
    service = _Stub(client)

    with pytest.raises(httpx.ConnectTimeout):
        asyncio.run(_post(service))

    assert client.timeouts[0] == 1.5


def test_a_dropped_event_delivery_is_counted_by_type_and_rate_limited(monkeypatch) -> None:
    """投递真丢的帧原来只落 `logger.debug('task event callback delivery skipped')`。

    `entry_callback_error_total` 数的是投递异常次数：一次批量失败回落逐条、逐条再失败，
    同一帧在里面算两笔，所以"丢了哪一类几帧"必须有自己的账。限流沿用同一条车道的 60 s。
    """
    import main.service.runtime_service as module

    logged: list[str] = []

    class _Recorder:
        def warning(self, message: str, *args) -> None:
            logged.append(str(message).format(*args) if args else str(message))

    monkeypatch.setattr(module, 'logger', _Recorder())
    client = _RaisingClient(httpx.ConnectTimeout('loop stalled'))
    service = _Stub(client)

    asyncio.run(service._deliver_task_event_callback({'event_type': 'task.node.patch'}))
    asyncio.run(service._deliver_task_event_callback({'event_type': 'task.node.patch'}))
    asyncio.run(service._deliver_task_event_callback({'event_type': 'task.model.call'}))

    assert service._task_event_delivery_dropped_by_type == {'task.node.patch': 2, 'task.model.call': 1}
    assert service._task_event_stats['task_event_delivery_dropped_count'] == 3.0
    # 每一帧都得真的发过一遍才算"投递失败"，否则这条用例只是在测壳子里少了个方法。
    assert client.posts == 3
    assert service._callback_delivery_errors == {'ConnectTimeout': 3}
    drop_lines = [text for text in logged if 'delivery dropped' in text]
    assert len(drop_lines) == 1
    # 限流那一行印的是"当时"的归属，后续丢弃照旧进账（上面的 by_type=3 笔）——
    # 与 `_note_callback_delivery_error` 同一语义：计数不限流，日志限流。
    assert 'latest=task.node.patch' in drop_lines[0]
    assert "by_type={'task.node.patch': 1}" in drop_lines[0]
    assert 'pool=' in drop_lines[0]


def test_referrer_labels_name_the_pending_proactor_operation() -> None:
    """孤儿的 transport 只被一个挂起的 proactor 操作引用；recv / send / connect 三种
    形状修法完全不同，所以标签里必须带上方法名。"""

    class _Holder:
        def recv_into(self, *args):
            return None

    label = MainRuntimeService._census_referrer_label(MethodType(_Holder.recv_into, _Holder()))

    assert label == 'method<recv_into>'
