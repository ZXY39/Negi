"""机械拒发（发送前预检失败）被当成自愿阻塞声明，喂进无上限打回循环。

事故 task:3ea1ba0cb21a / node:4c49d7a1f2f5（2026-10-10）实测构成：
- 执行节点 41 跳全在 16:46:50–16:47:03（一次性打满），此后 **零** `task_model_calls` 行；
- `task_node_notifications` 里同一份「执行节点提交了 failed+blocked」出现 6 次
  （16:47:03 / 16:49:18 / 16:50:26 / 16:51:54 / 16:54:22 / 16:56:03），
  验收节点为此跑了 14 跳真实模型调用，直到我手工取消；
- `task_error_logs` 0 行、`task_node_pauses` 0 行——所以界面与心跳都看不见它。

阻塞理由原话：`token compression helper returned an empty summary after retries (limit 3)`，
声明摘要 `node send token preflight failed`。这条 refusal 在 `_token_preflight_failure`
里生成，**没有** `failure_disposition='pause'`，于是既走不到 `node_runner` 的错误暂停道
（`node_runner.py:651-671`），又满足阻塞核验闸门的入口判据
（`status=failed` + `delivery_status=blocked`，`node_runner.py:1997-2002`），
被打回后 `return await self.run_node(...)`（`node_runner.py:2151`）原地重跑——
同一个输入必然再撞同一次预检失败：零进展、零落账、无次数上限。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from main.models import NodeFinalResult
from main.runtime.node_runner import NodePausedError
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService

_PREFLIGHT_REASON = (
    'token compression helper returned an empty summary after retries (limit 3)'
)


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"backend must not be reached: {kwargs!r}")


def _make_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="web",
    )
    service._assert_worker_available = lambda: None
    return service


def _preflight_failure_result() -> NodeFinalResult:
    return ReActToolLoop._token_preflight_failure(reason=_PREFLIGHT_REASON)


def _install_mechanical_refusal(service: MainRuntimeService) -> list[int]:
    """把 ReAct 道换成「每次进来都立刻机械拒发」，并数被进入的次数。"""
    entries: list[int] = []

    async def fake_run(*args, **kwargs) -> NodeFinalResult:
        entries.append(len(entries) + 1)
        return _preflight_failure_result()

    service.node_runner._react_loop.run = fake_run
    return entries


def _install_scripted_verifier(service: MainRuntimeService, verdicts: list[NodeFinalResult]) -> list[str]:
    calls: list[str] = []
    remaining = list(verdicts)

    async def fake_run_nested(task_id: str, node_id: str) -> NodeFinalResult:
        calls.append(node_id)
        verdict = remaining.pop(0) if len(remaining) > 1 else remaining[0]
        service.log_service.update_node_status(
            task_id,
            node_id,
            status=verdict.status,
            final_output=str(verdict.answer or verdict.summary or ""),
            failure_reason="" if verdict.status == "success" else str(verdict.blocking_reason or verdict.summary or ""),
        )
        return verdict

    service.node_runner._run_nested_node = fake_run_nested
    return calls


def _reject_verdict() -> NodeFinalResult:
    return NodeFinalResult(
        status="failed",
        delivery_status="final",
        summary="阻塞不成立",
        answer="阻塞不成立",
        evidence=[],
        remaining_work=["继续完成剩余工作后重新提交"],
        blocking_reason="预检失败不是阻塞，请重跑",
    )


def _unverifiable_verdict() -> NodeFinalResult:
    return NodeFinalResult(
        status="failed",
        delivery_status="blocked",
        summary="无法核验",
        answer="无法核验",
        evidence=[],
        remaining_work=[],
        blocking_reason="核验所需材料不可读",
    )


# ---------------------------------------------------------------------------
# C1：机械拒发的回包形态本身必须带可恢复暂停的处置档
# ---------------------------------------------------------------------------


def test_preflight_failure_carries_pause_disposition() -> None:
    result = _preflight_failure_result()
    assert str(result.failure_disposition or "").strip().lower() == "pause", (
        "发送前预检失败是运行时拒发，不是模型的自愿阻塞声明；"
        "不带 pause 处置就会被阻塞核验闸门收走并无限打回"
    )


# ---------------------------------------------------------------------------
# C2：机械拒发不得唤起验收核验，必须落错误行 + 节点暂停
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mechanical_refusal_pauses_instead_of_entering_blocked_gate(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    try:
        record = await service.create_task("preflight refusal recovery probe", session_id="web:shared")
        root = service.get_node(record.root_node_id)

        react_entries = _install_mechanical_refusal(service)
        # 基线上这条道会真去唤起验收：脚本给「打回、打回、无法核验」三张判词，
        # 让无上限循环在测试里能收口（第三张放行执行节点的失败）。
        verifier_calls = _install_scripted_verifier(
            service,
            [_reject_verdict(), _reject_verdict(), _unverifiable_verdict()],
        )

        with pytest.raises(NodePausedError):
            await service.node_runner.run_node(record.task_id, root.node_id)

        # 零验收回合：闸门根本不该看见这条 refusal。
        assert verifier_calls == []
        # 只进 ReAct 道一次：没有原地重跑循环。
        assert react_entries == [1]

        error_rows = list(service.log_service.list_task_node_error_logs(record.task_id, root.node_id) or [])
        assert error_rows, "机械拒发必须留下错误行，否则界面与心跳都读不到"
        assert any(_PREFLIGHT_REASON in str(getattr(row, "error_text", "") or "") for row in error_rows)

        pauses = list(service.store.list_task_node_pauses(record.task_id) or [])
        assert any(str(getattr(item, "node_id", "") or "") == root.node_id for item in pauses)
        assert service.get_node(root.node_id).status == "in_progress"
    finally:
        await service.close()
