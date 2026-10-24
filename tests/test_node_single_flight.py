"""P7 单飞：同一节点同一时刻只允许一个 `run_node` 回合同飞。

事故形状：调度器在 entry.future 已结束而节点仍非终态时会丢掉 entry 并允许重派
（`TaskNodeDispatcher._get_or_create_entry`），于是旧回合还活着、第二个 `run_node`
已经进来，两个协程各自消费同一份信箱与运行帧。守卫长在回合入口本身，不靠调度器补。

判据是「后来者阻塞到第一回合结束」：成功同值、暂停同型、取消同型。若改成立刻返回
一个 in-flight 回执，上层会把回执当成这一回合的结论（甚至把节点签成终态），而真正的
回合还在跑；改成抛异常又会被调用方当成节点失败。
"""

from __future__ import annotations

import asyncio

import pytest

from main.errors import NodePausedError
from main.models import NodeFinalResult
from main.runtime.node_runner import NodeRunner

TASK_ID = 'task:single-flight'
NODE_ID = 'node:exec'


def _bare_runner() -> NodeRunner:
    runner = object.__new__(NodeRunner)
    runner._node_turn_gates = {}  # type: ignore[attr-defined]
    return runner


def test_concurrent_run_node_executes_exactly_one_turn() -> None:
    """两个并发入口只跑一个回合；后来者等到的是第一回合的真实结果。"""
    runner = _bare_runner()
    started = asyncio.Event()
    release = asyncio.Event()
    model_calls: list[int] = []

    async def fake_turn(task_id: str, node_id: str) -> NodeFinalResult:
        model_calls.append(1)
        started.set()
        await release.wait()
        return NodeFinalResult(status='success', summary='turn done')

    runner._run_node_turn = fake_turn  # type: ignore[attr-defined]

    async def scenario() -> tuple[NodeFinalResult, NodeFinalResult]:
        first = asyncio.create_task(runner.run_node(TASK_ID, NODE_ID))
        await started.wait()
        second = asyncio.create_task(runner.run_node(TASK_ID, NODE_ID))
        # 让第二个协程走到守卫上；它必须停在那里，不能自己再开一个回合。
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(model_calls) == 1, '同一节点同时跑了两个回合'
        assert not second.done(), '第二个并发入口必须等第一回合结束，不能自造结论'
        release.set()
        return await first, await second

    first_result, second_result = asyncio.run(scenario())

    assert len(model_calls) == 1
    assert first_result.summary == 'turn done'
    assert second_result.summary == 'turn done'
    assert runner._node_turn_gates == {}


def test_concurrent_waiter_receives_the_first_turns_pause() -> None:
    """第一回合以暂停收场时，等待者收到同一类暂停，而不是被当成一次节点失败。"""
    runner = _bare_runner()
    release = asyncio.Event()
    started = asyncio.Event()
    model_calls: list[int] = []

    async def fake_turn(task_id: str, node_id: str) -> NodeFinalResult:
        model_calls.append(1)
        started.set()
        await release.wait()
        raise NodePausedError(TASK_ID, NODE_ID)

    runner._run_node_turn = fake_turn  # type: ignore[attr-defined]

    async def scenario() -> tuple[BaseException, BaseException]:
        first = asyncio.create_task(runner.run_node(TASK_ID, NODE_ID))
        await started.wait()
        second = asyncio.create_task(runner.run_node(TASK_ID, NODE_ID))
        for _ in range(5):
            await asyncio.sleep(0)
        assert len(model_calls) == 1
        release.set()
        outcomes: list[BaseException] = []
        for task in (first, second):
            with pytest.raises(NodePausedError) as caught:
                await task
            outcomes.append(caught.value)
        return outcomes[0], outcomes[1]

    first_error, second_error = asyncio.run(scenario())

    assert isinstance(first_error, NodePausedError)
    assert isinstance(second_error, NodePausedError)
    assert len(model_calls) == 1
    assert runner._node_turn_gates == {}, '异常收场同样要摘除登记，不能泄漏成永久暂停门'


def test_gate_is_released_after_the_turn_so_the_next_turn_can_start() -> None:
    """回合结束后登记表清空：终态后的合法新回合（验收打回/resume）照常开跑。"""
    runner = _bare_runner()
    model_calls: list[int] = []

    async def fake_turn(task_id: str, node_id: str) -> NodeFinalResult:
        model_calls.append(1)
        return NodeFinalResult(status='success', summary=f'turn {len(model_calls)}')

    runner._run_node_turn = fake_turn  # type: ignore[attr-defined]

    first = asyncio.run(runner.run_node(TASK_ID, NODE_ID))
    assert runner._node_turn_gates == {}, '回合结束后登记表必须清空，锁不能泄漏'

    second = asyncio.run(runner.run_node(TASK_ID, NODE_ID))

    assert len(model_calls) == 2, '顺序重入必须能正常开新回合'
    assert (first.summary, second.summary) == ('turn 1', 'turn 2')
