"""验收握手拒收预算回归测试。

修复缺陷：执行节点重新提交时 _set_execution_waiting_acceptance_state 的
默认参数把 rejection_count 重置为 0，导致连续拒收永远达不到上限，任务在
"执行→验收拒绝→重跑→再提交"之间无限循环。预算必须在重新提交间保留，
仅当调用方显式传入计数时才覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

from main.runtime.node_runner import ACCEPTANCE_HANDSHAKE_KEY, NodeRunner


class _StoreStub:
    def __init__(self, execution) -> None:
        self._execution = execution

    def get_node(self, node_id: str):
        if str(node_id) == "exec-1":
            return self._execution
        return SimpleNamespace(node_id=node_id, metadata={})

    def get_task(self, task_id: str):
        return None


def _make_runner(execution) -> tuple[NodeRunner, list[dict]]:
    runner = object.__new__(NodeRunner)
    recorded: list[dict] = []

    def _record_update(**kwargs) -> None:
        recorded.append(kwargs)

    runner._store = _StoreStub(execution)  # type: ignore[attr-defined]
    runner._update_execution_acceptance_handshake = _record_update  # type: ignore[attr-defined]
    runner._acceptance_updates_task_final_acceptance = lambda **kwargs: False  # type: ignore[attr-defined]
    return runner, recorded


def _execution_with_handshake(rejection_count: int | None) -> SimpleNamespace:
    if rejection_count is None:
        metadata: dict = {}
    else:
        metadata = {
            ACCEPTANCE_HANDSHAKE_KEY: {
                "state": "waiting_acceptance",
                "rejection_count": rejection_count,
                "max_rejections": 3,
            }
        }
    return SimpleNamespace(node_id="exec-1", metadata=metadata)


def _resubmit(runner: NodeRunner, **overrides) -> dict:
    kwargs = dict(
        task_id="task-1",
        execution_node_id="exec-1",
        acceptance_node_id="acc-1",
        result_ref="ref-1",
        result_summary="sum",
    )
    kwargs.update(overrides)
    runner._set_execution_waiting_acceptance_state(**kwargs)  # type: ignore[attr-defined]


def test_resubmission_without_count_preserves_rejection_budget() -> None:
    runner, recorded = _make_runner(_execution_with_handshake(2))
    _resubmit(runner)
    assert recorded, "handshake update must be recorded"
    assert recorded[0]["rejection_count"] == 2, "重新提交不得清零既有拒收计数"


def test_explicit_rejection_count_still_wins() -> None:
    runner, recorded = _make_runner(_execution_with_handshake(2))
    _resubmit(runner, rejection_count=1)
    assert recorded[0]["rejection_count"] == 1


def test_fresh_handshake_starts_at_zero() -> None:
    runner, recorded = _make_runner(_execution_with_handshake(None))
    _resubmit(runner)
    assert recorded[0]["rejection_count"] == 0
