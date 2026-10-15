"""观测旁路故障不得取消被观测工具，且一手错误信息要能走到文本层。

事故（`task:869c064afdb2`，2026-10-15 16:47:58–16:48:01）：看门狗每次轮询的暂停检查
是一次阅读（`_on_tool_watchdog_poll` → `_check_pause_or_cancel` → `get_task`），而它被裸
`await`。一次 `sqlite3.OperationalError: disk I/O error` 因此穿透到
`run_tool_with_watchdog` 的 `except BaseException`，触发 `request_tool_cancellation`，
把两条健康的 spawn 轮打死；12 条 entry 落成 `Error: CancelledError`（其中 3 个孩子早已
success），5 分钟后收尸器照这句假话把 6 个仍在跑的节点判成孤儿杀掉。

同文件的 `_safe_summarized_snapshot` 早已为完全相同的理由单独包过一次——两个回调各包一层
就是形状错了的证据。本组用例把护栏收到观测通道的唯一入口 `_safe_observe` 上，并要求取消
诊断（`watchdog_aborted:<tool>`）与因果链可达文本：以前 `reason` 只进
`cancel_token.cancel(reason=...)`（还被 `try/except pass` 包住），全仓没有第二个读者。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

from g3ku.runtime.tool_watchdog import (
    _attach_watchdog_cancel_reason,
    _safe_summarized_snapshot,
    _wait_for_task_window,
    read_watchdog_cancel_reason,
    run_tool_with_watchdog,
)
from main.errors import (
    NodePausedError,
    TaskPausedError,
    describe_exception,
    describe_exception_chain,
)

_POLL_FAST = {'tool_watchdog': {'poll_interval_seconds': 0.2, 'stop_grace_seconds': 0.0}}


async def _store_blip(*_args, **_kwargs):
    """最像事故的一拍：观测车道读库读挂了，工具本身完全健康。"""
    raise sqlite3.OperationalError('disk I/O error')


async def _wait_window_with_failing_poll(*, tool_body, on_poll):
    async def _slow_tool():
        await asyncio.sleep(0.6)
        return 'tool-result'

    task = asyncio.create_task(_slow_tool())
    try:
        result = await _wait_for_task_window(
            task=task,
            tool_name='spawn_child_nodes',
            started_at=time.monotonic(),
            snapshot_supplier=None,
            poll_interval_seconds=0.05,
            handoff_after_seconds=10.0,
            text_char_limit=280,
            list_limit=3,
            on_poll=on_poll,
        )
    finally:
        if not task.done():
            task.cancel()
    return result, task


async def test_store_blip_in_on_poll_does_not_abort_running_tool() -> None:
    """每次轮询都抛 OperationalError 时，工具必须照常跑完——这是本次事故的正门。"""
    polls = {'count': 0}

    async def _on_poll(_payload):
        polls['count'] += 1
        raise sqlite3.OperationalError('disk I/O error')

    result, task = await _wait_window_with_failing_poll(tool_body=None, on_poll=_on_poll)
    assert polls['count'] >= 1, '轮询期间必须真的调用过观测回调'
    assert result.completed is True, '观测旁路的读失败不得中断工具执行'
    assert result.value == 'tool-result'
    assert not task.cancelled(), '健康的在飞工具不得被看门狗取消'


async def test_sync_on_poll_store_blip_also_degrades() -> None:
    """同步形态的观测回调同样不能把工具打死（`_maybe_await` 两条形状都要过）。"""

    def _on_poll(_payload):
        raise sqlite3.OperationalError('disk I/O error')

    result, _task = await _wait_window_with_failing_poll(tool_body=None, on_poll=_on_poll)
    assert result.completed is True
    assert result.value == 'tool-result'


async def test_declared_control_signal_still_propagates() -> None:
    """冻结信号不是仪表故障：它必须照旧冒泡，被降级的只有「取消工具」这一件事。"""

    async def _on_poll(_payload):
        raise TaskPausedError('task:demo')

    raised = ''
    try:
        await run_tool_with_watchdog(
            asyncio.sleep(1.0),
            tool_name='spawn_child_nodes',
            runtime_context=dict(_POLL_FAST),
            on_poll=_on_poll,
            observation_control_signals=(TaskPausedError, NodePausedError),
        )
    except TaskPausedError as exc:
        raised = type(exc).__name__
    assert raised == 'TaskPausedError', '暂停信号被观测护栏吞掉了'


async def test_cancelled_error_from_instrumentation_is_never_swallowed() -> None:
    """非 Exception 的 BaseException 永远冒泡，不在降级范围内。"""

    async def _on_poll(_payload):
        raise asyncio.CancelledError

    propagated = False
    try:
        await _wait_for_task_window(
            task=asyncio.create_task(asyncio.sleep(0.05)),
            tool_name='some_tool',
            started_at=time.monotonic(),
            snapshot_supplier=None,
            poll_interval_seconds=0.05,
            handoff_after_seconds=10.0,
            text_char_limit=280,
            list_limit=3,
            on_poll=_on_poll,
        )
    except asyncio.CancelledError:
        propagated = True
    assert propagated, 'CancelledError 不得被观测护栏吞掉'


async def test_summarizer_failure_is_inside_the_guard() -> None:
    """摘要这一步以前在 try 外面：它自己抛照样会穿透成一次工具取消。"""

    class _ExplodingDict(dict):
        def get(self, *_args, **_kwargs):
            raise sqlite3.OperationalError('disk I/O error while summarizing')

    previous = {'snapshot_type': 'runtime', 'summary_text': '旧快照'}

    async def _supplier():
        return _ExplodingDict(task={}, root_node={})

    degraded = await _safe_summarized_snapshot(
        snapshot_supplier=_supplier,
        tool_name='spawn_child_nodes',
        text_char_limit=280,
        list_limit=3,
        fallback=previous,
    )
    assert degraded == previous, '摘要失败必须回退到最近一次有效快照，而不是向外抛'

    async def _slow_tool():
        await asyncio.sleep(0.3)
        return 'ok'

    task = asyncio.create_task(_slow_tool())
    try:
        result = await _wait_for_task_window(
            task=task,
            tool_name='spawn_child_nodes',
            started_at=time.monotonic(),
            snapshot_supplier=_supplier,
            poll_interval_seconds=0.05,
            handoff_after_seconds=10.0,
            text_char_limit=280,
            list_limit=3,
        )
    finally:
        if not task.done():
            task.cancel()
    assert result.completed is True, '摘要失败不得中断工具执行'


# --- I-10：一手错误信息 -----------------------------------------------------


def test_describe_exception_drops_the_original_fault() -> None:
    """钉住现状：`describe_exception` 只看最后一跳，账本里因此只剩裸异常名。"""
    original = None
    try:
        raise sqlite3.OperationalError('disk I/O error')
    except sqlite3.OperationalError as exc:
        original = exc
    try:
        raise original
    except sqlite3.OperationalError:
        try:
            raise RuntimeError('hold probe failed')
        except RuntimeError as outer:
            flat = describe_exception(outer)
    assert flat == 'RuntimeError: hold probe failed'
    assert 'disk I/O error' not in flat


def test_describe_exception_chain_keeps_both_links() -> None:
    try:
        raise sqlite3.OperationalError('disk I/O error')
    except sqlite3.OperationalError as cause:
        try:
            raise RuntimeError('hold probe failed') from cause
        except RuntimeError as outer:
            rendered = describe_exception_chain(outer)
    assert 'hold probe failed' in rendered
    assert 'disk I/O error' in rendered, '一手故障必须出现在文本里'
    assert ' <- ' in rendered


def test_describe_exception_chain_honors_suppressed_context() -> None:
    try:
        raise ValueError('must stay hidden')
    except ValueError:
        try:
            raise KeyError('only this') from None
        except KeyError as outer:
            assert describe_exception_chain(outer) == "KeyError: 'only this'"


def test_describe_exception_chain_caps_depth_without_recursing() -> None:
    link = None
    for index in range(12):
        try:
            if link is None:
                raise ValueError('leaf')
            raise TypeError('link-%d' % index) from link
        except (ValueError, TypeError) as exc:
            link = exc
    capped = describe_exception_chain(link, max_depth=3)
    assert capped.count(' <- ') == 3, capped
    assert '超过 3 跳' in capped, capped


def test_watchdog_cancel_reason_reaches_text() -> None:
    """取消诊断必须可读回来：以前 `reason` 生成完就没有第二个读者。"""
    exc = RuntimeError('instrumentation exploded')
    _attach_watchdog_cancel_reason(exc, reason='watchdog_aborted:spawn_child_nodes')
    read_back = read_watchdog_cancel_reason(exc)
    assert read_back == 'watchdog_aborted:spawn_child_nodes'
    assert 'spawn_child_nodes' in read_back
    assert read_watchdog_cancel_reason(ValueError('untagged')) == ''


async def test_reason_not_attached_when_the_tool_already_finished() -> None:
    """正常跑完的这条路上没有取消诊断：`except BaseException` 根本没进，
    因此 `title=tool_watchdog_cancel_requested` 也不会出现。"""

    async def _quick_tool():
        return 'done'

    outcome = await run_tool_with_watchdog(
        _quick_tool(),
        tool_name='quick_tool',
        runtime_context=dict(_POLL_FAST),
    )
    assert outcome.completed is True and outcome.value == 'done'
