"""验收 bootstrap 定稿与「指针而非正文」回归（task:c7f1dbfae6e2 阶段 C，2026-09-19）。

改造前的形态：`_refresh_acceptance_node_prompt` 每次刷新都把最新一次交付的正文写回
`node.prompt`/`node.input`，交接通知也整篇嵌入 `result.answer`。于是验收节点的持久
历史里，最该稳定的 bootstrap 成了移动靶——scaffold 头探针比对的正是首两条记录，
prefix 因此随每份交付漂移（`fallback_seed_prefix_drift`），而上下文里堆满历次交付
全文后，核验方看到的不再是"当前这一份提交"。

本文件锁定改造后的契约：
- C1：bootstrap（system + user 首两条）跨提交逐字节不变；
- C2：交接通知只给 ref + 有界摘要，不嵌交付全文；
- C3：创建期摘要同样有界；
- C4：每轮的变化量全部落在只进本轮的尾块里。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from main.models import NodeFinalResult, normalize_final_acceptance_metadata
from main.runtime.node_runner import _ACCEPTANCE_SUMMARY_CHARS
from main.service.runtime_service import MainRuntimeService

_LONG_BODY = "交付正文-" * 4000


class _DummyChatBackend:
    async def chat(self, **kwargs):
        raise AssertionError(f"chat backend should not be called in this test: {kwargs!r}")


def _build_service(tmp_path: Path) -> MainRuntimeService:
    service = MainRuntimeService(
        chat_backend=_DummyChatBackend(),
        workspace_root=tmp_path,
        store_path=tmp_path / "runtime.sqlite3",
        files_base_dir=tmp_path / "tasks",
        artifact_dir=tmp_path / "artifacts",
        governance_store_path=tmp_path / "governance.sqlite3",
        execution_mode="embedded",
        execution_model_refs=["fake"],
        acceptance_model_refs=["fake"],
    )

    async def _noop_enqueue_task(_task_id: str) -> None:
        return None

    service.global_scheduler.enqueue_task = _noop_enqueue_task
    return service


async def _final_acceptance_nodes(service: MainRuntimeService, *, title: str):
    record = await service.create_task(
        title,
        session_id="web:shared",
        metadata={"final_acceptance": {"required": True, "prompt": "verify root output"}},
    )
    task = service.get_task(record.task_id)
    root = service.get_node(record.root_node_id)
    assert task is not None and root is not None
    acceptance_id = str(
        normalize_final_acceptance_metadata((task.metadata or {}).get("final_acceptance")).node_id or ""
    ).strip()
    acceptance = service.store.get_node(acceptance_id)
    assert acceptance is not None
    return record.task_id, root, acceptance


def _submit(text: str) -> NodeFinalResult:
    return NodeFinalResult(
        status="success",
        delivery_status="final",
        summary=text,
        answer=text,
        evidence=[],
        remaining_work=[],
        blocking_reason="",
    )


def _turn_tail(messages: list[dict[str, object]]) -> str:
    for item in reversed(list(messages or [])):
        content = str((item or {}).get("content") or "")
        if "本轮验收上下文" in content:
            return content
    return ""


@pytest.mark.asyncio
async def test_acceptance_bootstrap_head_is_identical_across_resubmissions(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root, acceptance = await _final_acceptance_nodes(service, title="bootstrap head stability")
        first = await service.node_runner._build_messages(task=service.store.get_task(task_id), node=acceptance)
        head_first = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in first[:2]]

        service.node_runner._maybe_start_acceptance_handshake(
            task=service.store.get_task(task_id),
            node=service.store.get_node(root.node_id),
            result=_submit(_LONG_BODY),
        )
        second = await service.node_runner._build_messages(task=service.store.get_task(task_id), node=acceptance)
        head_second = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in second[:2]]
        assert head_second == head_first

        service.node_runner._maybe_start_acceptance_handshake(
            task=service.store.get_task(task_id),
            node=service.store.get_node(root.node_id),
            result=_submit("第二次提交：换了一份完全不同的交付"),
        )
        third = await service.node_runner._build_messages(task=service.store.get_task(task_id), node=acceptance)
        assert [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in third[:2]] == head_first
        # 变化量必须全部落在尾块：否则头探针又会失去比对基准。
        tail = _turn_tail(third)
        assert "第二次提交：换了一份完全不同的交付" in tail
        assert _LONG_BODY not in tail
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_acceptance_handoff_notice_carries_refs_instead_of_the_full_answer(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        task_id, root, acceptance = await _final_acceptance_nodes(service, title="bounded handoff")

        handoff = service.node_runner._maybe_start_acceptance_handshake(
            task=service.store.get_task(task_id),
            node=service.store.get_node(root.node_id),
            result=_submit(_LONG_BODY),
        )
        assert handoff is not None

        notices = list(service.store.list_task_node_notifications(task_id, acceptance.node_id) or [])
        assert notices, "交接必须留下一条持久通知"
        message = str(notices[-1].message or "")
        assert _LONG_BODY not in message
        assert "待验提交结果载荷 ref" in message
        assert len(message) <= _ACCEPTANCE_SUMMARY_CHARS + 500
        # 有界摘要仍要能认出这是哪一次提交。
        assert "交付正文" in message
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_acceptance_bootstrap_bounds_the_creation_time_output(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    try:
        record = await service.create_task("eager bootstrap bound", session_id="web:shared")
        task = service.get_task(record.task_id)
        root = service.get_node(record.root_node_id)
        assert task is not None and root is not None
        service.log_service.update_node_status(
            record.task_id,
            root.node_id,
            status="success",
            final_output=_LONG_BODY,
        )

        acceptance = service.node_runner.create_acceptance_node(
            task=service.store.get_task(record.task_id),
            accepted_node=service.store.get_node(root.node_id),
            goal="accept:eager",
            acceptance_prompt="verify root output",
        )

        # 交付正文以「摘要 + ref」进 bootstrap，绝不整篇嵌入。
        assert _LONG_BODY not in acceptance.prompt
        assert "子节点输出 ref：artifact:" in acceptance.prompt

        composed = service.node_runner._compose_acceptance_prompt(
            acceptance_prompt="verify root output",
            node_output=_LONG_BODY,
            node_output_ref="artifact:output",
            result_payload_ref="artifact:payload",
            evidence_summary="",
        )
        assert "摘要截断至" in composed
        assert _LONG_BODY not in composed
    finally:
        await service.close()
