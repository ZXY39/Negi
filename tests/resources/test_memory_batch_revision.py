"""记忆批次改稿通道：预览可以重交，最后一版才算数。

实盘形状：模型把行号数错（19 而不是 18），自己从 diff 里看出误覆盖后重交修正批次，运行时却把
第二次提交判成 "must call memory_apply_batch exactly once"，预览直接 unbuildable，确认也救不
回来，整批按 protocol/rejected 停车。改稿是合法动作：闸门只该管「落地前必须确认」，不该管
「一辈子只能交一次」。
"""
from __future__ import annotations

import importlib
import re
from datetime import date
from pathlib import Path

import pytest


def _module():
    return importlib.import_module("g3ku.agent.memory_agent_runtime")


def _cfg():
    schema = importlib.import_module("g3ku.config.schema")
    cfg = schema.MemoryToolsConfig()
    cfg.document.document_max_chars = 20000
    cfg.document.summary_max_chars = 300
    cfg.queue.max_wait_seconds = 0
    return cfg


def _manager(tmp_path: Path):
    return _module().MemoryManager(tmp_path, _cfg())


def _add(manager, *, body: str, minimal: str) -> str:
    row = manager._memory_repo.create_memory(
        memory_body=body,
        minimal_memory=minimal,
        source="self",
        from_user=False,
        now_iso="2026-04-25T00:00:00+08:00",
    )
    return str(row["memory_id"])


def _paint(manager) -> None:
    manager._rebuild_memory_snapshot_from_sqlite(now_iso="2026-04-25T00:00:30+08:00")


def _session(manager, *, op: str = "write"):
    mod = _module()
    return mod._MemoryToolSession(
        snapshot_text=manager.snapshot_text(),
        notes_dir=manager.notes_dir,
        index_dir=manager.index_dir,
        batch_op=op,
    )


def _batch(manager):
    mod = _module()
    return mod.MemoryBatch(
        op="write",
        items=[
            mod.MemoryQueueRequest(
                op="write",
                decision_source="user",
                payload_text="改掉对应那条记忆",
                created_at="2026-04-25T00:00:32+08:00",
                request_id="write_1",
            )
        ],
    )


def _tools(manager, session):
    return {tool.name: tool for tool in manager._memory_agent_tools(session)}


def test_resubmitting_replaces_the_preview_and_lands_the_last_batch(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, body="第一条旧正文", minimal="旧->一")
        _add(manager, body="第二条旧正文", minimal="旧->二")
        _paint(manager)
        original = manager.snapshot_text().splitlines()
        assert len(original) == 2
        session = _session(manager)
        tools = _tools(manager, session)

        first = tools["memory_apply_batch"].invoke(
            {"rewrites": [{"line": 1, "content": "被覆盖的甲版正文", "minimal_memory": "甲->版"}]}
        )
        assert first["ok"] is True
        assert first["status"] == "batch_previewed"
        assert first["replaced_previous"] is False
        assert first["preview"]["status"] == "built"

        second = tools["memory_apply_batch"].invoke(
            {"rewrites": [{"line": 2, "content": "最终落地的乙版正文", "minimal_memory": "乙->版"}]}
        )
        assert second["ok"] is True, second
        assert second["status"] == "batch_previewed"
        assert second["replaced_previous"] is True
        assert second["preview"]["status"] == "built", second
        assert "voided" in second["next_step"]

        confirmed = tools["memory_apply_confirm"].invoke({})
        assert confirmed["status"] == "batch_confirmed"

        validated = manager._validate_candidate_state(
            batch=_batch(manager),
            before_text=manager.snapshot_text(),
            session=session,
        )
        document = validated.document_text
        assert "最终落地的乙版正文" in document
        assert "被覆盖的甲版正文" not in document, "重交后落地的只能是最后一版"
        assert original[0] in document, "第 1 行没被这一版改，必须原样留着"
        assert original[1] not in document, "第 2 行才是这一版改的那条"
    finally:
        manager.close()


def test_resubmitting_voids_the_confirmation_given_to_the_previous_preview(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, body="旧正文一", minimal="旧->一")
        _add(manager, body="旧正文二", minimal="旧->二")
        _paint(manager)
        session = _session(manager)
        tools = _tools(manager, session)

        tools["memory_apply_batch"].invoke(
            {"rewrites": [{"line": 1, "content": "新正文一", "minimal_memory": "新->一"}]}
        )
        assert tools["memory_apply_confirm"].invoke({})["status"] == "batch_confirmed"

        tools["memory_apply_batch"].invoke(
            {"rewrites": [{"line": 2, "content": "新正文二", "minimal_memory": "新->二"}]}
        )
        assert session.confirm_count == 0, "重交之后旧确认必须作废，否则没读过的批次会直接落地"

        mod = _module()
        with pytest.raises(mod._MemoryAgentValidationError, match="memory_apply_confirm is required"):
            manager._validate_candidate_state(
                batch=_batch(manager),
                before_text=manager.snapshot_text(),
                session=session,
            )
    finally:
        manager.close()


def test_a_batch_that_does_not_build_is_not_staged(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, body="只有一条", minimal="一->一")
        _paint(manager)
        session = _session(manager)
        tools = _tools(manager, session)

        rejected = tools["memory_apply_batch"].invoke(
            {"rewrites": [{"line": 9, "content": "越界正文", "minimal_memory": "越界->x"}]}
        )
        assert rejected["ok"] is False
        assert "outside the memory block" in rejected["errors"]["batch"]
        assert rejected["preview"]["status"] == "unbuildable"
        assert "call memory_apply_batch again" in rejected["next_step"]
        assert session.applied_batch is None, "建不起来的批次不能留在暂存位，否则确认会重试一个已知失败的载荷"

        confirmed = tools["memory_apply_confirm"].invoke({})
        assert confirmed["ok"] is False
        assert "stage a batch before confirming" in confirmed["errors"]["batch"]
    finally:
        manager.close()


def test_prompt_numbers_resident_lines_and_leaves_navigation_unnumbered(tmp_path: Path) -> None:
    markdown = importlib.import_module("g3ku.agent.markdown_memory")
    manager = _manager(tmp_path)
    try:
        snapshot = "261018:第一条\n261018:第二条\n" + markdown.render_consolidation_block(
            [("分类说明", "note_cat")]
        )
        prompt = manager._memory_agent_user_prompt(
            batch=_batch(manager),
            snapshot_text=snapshot,
            repair_reason="",
        )

        assert "1| 261018:第一条" in prompt
        assert "2| 261018:第二条" in prompt
        assert "以下归并区没有行号" in prompt
        assert "## 归并记忆" in prompt
        assert not re.search(r"^\d+\|\s*分类说明", prompt, re.MULTILINE), "归并区的行没有行号可指，不能编号"
    finally:
        manager.close()


def test_content_that_copies_the_rendered_prefix_is_cleaned(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    try:
        _add(manager, body="旧正文", minimal="旧->一")
        _paint(manager)
        session = _session(manager)
        tools = _tools(manager, session)
        # 落地那行的日期前缀由运行时按 row.updated_at 现算，所以期望值跟着当天走；
        # 提交体里那份 261018 是「模型抄了渲染前缀」的样本，它必须被摘掉再重钉。
        stamped = date.today().strftime("%y%m%d")

        staged = tools["memory_apply_batch"].invoke(
            {"rewrites": [{"line": 1, "content": "1| 261018:改好之后的正文内容", "minimal_memory": "改->好"}]}
        )
        assert staged["preview"]["status"] == "built", staged
        tools["memory_apply_confirm"].invoke({})

        validated = manager._validate_candidate_state(
            batch=_batch(manager),
            before_text=manager.snapshot_text(),
            session=session,
        )
        document = validated.document_text
        assert f"{stamped}:改好之后的正文内容" in document
        assert "261018:改好之后的正文内容" not in document
        assert not re.search(r"\d{6}:\d{6}:", document), document
        assert "1|" not in document
    finally:
        manager.close()
