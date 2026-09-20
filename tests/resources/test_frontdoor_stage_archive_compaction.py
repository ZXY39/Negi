from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from g3ku.runtime.frontdoor import _ceo_runtime_ops as ops
from g3ku.runtime.frontdoor._ceo_create_agent_impl import CreateAgentCeoFrontDoorRunner
from g3ku.runtime.frontdoor.canonical_context import (
    combine_canonical_context,
    normalize_frontdoor_canonical_context,
)
from g3ku.runtime.frontdoor.raw_stage_renderer import retained_raw_stage_messages
from g3ku.runtime.stage_prompt_compaction import (
    STAGE_COMPACT_PREFIX,
    compact_stage_prompt_messages_in_place,
    completed_stage_blocks,
    retained_completed_stage_ids,
    stage_ref_candidates,
)


def _stage(index: int, *, visible: bool = True, key_refs=None, tool_call_ids=None) -> dict:
    stage = {
        "stage_id": f"frontdoor-stage-{index}",
        "stage_index": index,
        "stage_goal": f"目标 {index}",
        "status": "completed",
        "stage_kind": "normal",
        "mode": "自主执行",
        "completed_stage_summary": f"结论 {index}",
        "created_at": f"2026-09-20T0{index}:00:00+08:00",
        "finished_at": f"2026-09-20T0{index}:00:30+08:00",
        "key_refs": list(key_refs or []),
        "rounds": [],
    }
    if tool_call_ids:
        stage["rounds"] = [{"round_index": 1, "tool_call_ids": list(tool_call_ids), "tools": []}]
    if not visible:
        stage["context_visible"] = False
    return stage


def _ledger(stages: list[dict]) -> dict:
    return {"active_stage_id": "", "transition_required": False, "stages": stages}


def _prompt_messages(*, blocks_for: list[int], dialogue: int = 6) -> list[dict]:
    messages: list[dict] = [{"role": "system", "content": "基础提示"}]
    for index in blocks_for:
        payload = {"stage_index": index, "stage_goal": f"目标 {index}", "completed_stage_summary": f"结论 {index}"}
        messages.append({"role": "system", "content": f"{STAGE_COMPACT_PREFIX}\n{json.dumps(payload, ensure_ascii=False)}"})
    for position in range(dialogue):
        role = "user" if position % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"对话 {position}"})
    return messages


# ---- 收口标记：渲染层 -------------------------------------------------------


def test_completed_stage_blocks_skip_archived_stages() -> None:
    ledger = _ledger([_stage(1), _stage(2, visible=False), _stage(3)])
    blocks = completed_stage_blocks(ledger)
    indexes = sorted(json.loads(block["content"].split("\n", 1)[1])["stage_index"] for block in blocks)
    # 收口阶段不再逐轮渲染；可见阶段照常。
    assert indexes == [1, 3]


def test_archived_stages_do_not_consume_raw_window_slots() -> None:
    ledger = _ledger([_stage(1), _stage(2, visible=False), _stage(3), _stage(4), _stage(5), _stage(6)])
    retained = retained_completed_stage_ids(ledger, keep_latest=3)
    assert retained == {"frontdoor-stage-4", "frontdoor-stage-5", "frontdoor-stage-6"}
    raw_messages, raw_ids = retained_raw_stage_messages(ledger, keep_latest_completed_stages=3)
    assert raw_ids == retained
    # 收口阶段把名额让出来，否则近场执行细节会被"已经进过摘要"的阶段挤掉。
    assert len(raw_messages) == 3


def test_archive_flag_survives_normalize_and_combine() -> None:
    durable = normalize_frontdoor_canonical_context(_ledger([_stage(1), _stage(2, visible=False)]))
    by_id = {stage["stage_id"]: stage for stage in durable["stages"]}
    assert by_id["frontdoor-stage-2"]["context_visible"] is False
    assert "context_visible" not in by_id["frontdoor-stage-1"]

    combined = combine_canonical_context(durable, _ledger([]))
    combined_by_id = {stage["stage_id"]: stage for stage in combined["stages"]}
    assert combined_by_id["frontdoor-stage-2"]["context_visible"] is False


def test_archived_durable_stage_still_dedupes_against_turn_copy() -> None:
    """收口标记不得进入重叠签名，否则本轮副本会被当新阶段追加（stage_index 虚增）。"""
    durable = normalize_frontdoor_canonical_context(_ledger([_stage(7, visible=False)]))
    turn_copy = _ledger([{**_stage(1), "stage_id": "turn-1", "created_at": "2026-09-20T07:00:00+08:00", "finished_at": "2026-09-20T07:00:30+08:00", "stage_goal": "目标 7", "completed_stage_summary": "结论 7"}])
    combined = combine_canonical_context(durable, turn_copy)
    assert len(combined["stages"]) == 1
    assert combined["stages"][0]["context_visible"] is False


def test_warm_assembly_carries_no_compact_blocks_after_archive() -> None:
    """整条病灶的回归点：压缩后的种子 + 全量收口账本 → 请求体里不该再有阶段块。"""
    stages = [_stage(index, visible=False) for index in range(1, 398)] + [_stage(398), _stage(399), _stage(400)]
    ledger = _ledger(stages)
    seed = [
        {"role": "system", "content": "基础提示"},
        {"role": "assistant", "content": "[G3KU_TOKEN_COMPACT_V2]\n{\"kind\":\"frontdoor_token_compaction_llm\"}\n\n摘要正文"},
        {"role": "user", "content": "刚压缩了，结构是什么"},
        {"role": "assistant", "content": "直接回答"},
    ]
    parts = compact_stage_prompt_messages_in_place(seed, stage_state=ledger, keep_latest_completed_stages=3)
    rewritten = [*parts["prefix"], *parts["rewritten"]]
    assert [item for item in rewritten if str(item.get("content") or "").startswith(STAGE_COMPACT_PREFIX)] == []
    assert len(rewritten) == 4

    # 对照组：同一份账本未收口时，块会成批长回来——这正是收口要消除的形态。
    visible_ledger = _ledger([{key: value for key, value in stage.items() if key != "context_visible"} for stage in stages])
    untrimmed = compact_stage_prompt_messages_in_place(seed, stage_state=visible_ledger, keep_latest_completed_stages=3)
    grown = [item for item in [*untrimmed["prefix"], *untrimmed["rewritten"]] if str(item.get("content") or "").startswith(STAGE_COMPACT_PREFIX)]
    assert len(grown) == 397


def test_archived_stage_frames_are_stripped_without_block_replacement() -> None:
    """收口阶段的工具肉身若被冷路径重投影回来，按原位移除且不再补块——这就是收口的信息损失边界。"""
    ledger = _ledger([_stage(1, tool_call_ids=["c1"]), _stage(2, visible=False, tool_call_ids=["c2"])])
    seed = [
        {"role": "system", "content": "基础提示"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "exec"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "结果 1"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c2", "function": {"name": "exec"}}],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "结果 2"},
        {"role": "user", "content": "当前问题"},
    ]
    parts = compact_stage_prompt_messages_in_place(seed, stage_state=ledger, keep_latest_completed_stages=1)
    rewritten = [*parts["prefix"], *parts["rewritten"]]
    blocks = [item for item in rewritten if str(item.get("content") or "").startswith(STAGE_COMPACT_PREFIX)]
    # stage 1 在保留窗口内（肉身留在原位）；stage 2 已收口：肉身移除、不补块。
    assert blocks == []
    assert any(str(item.get("tool_call_id") or "") == "c1" for item in rewritten)
    assert not any(str(item.get("tool_call_id") or "") == "c2" for item in rewritten)


# ---- 候选与逐字回填 ---------------------------------------------------------


def test_stage_ref_candidates_are_ordered_deduped_and_numbered(tmp_path: Path) -> None:
    live = tmp_path / "report.md"
    live.write_text("x", encoding="utf-8")
    ledger = _ledger(
        [
            _stage(1, key_refs=[{"ref": str(live), "note": "报告"}, {"ref": "task:aaaa1111", "note": "任务"}]),
            _stage(2, key_refs=[{"ref": str(live), "note": "报告（更新说明）"}]),
            _stage(3, visible=False, key_refs=[{"ref": "artifact:bbbb2222", "note": "已收口"}]),
        ]
    )
    candidates = stage_ref_candidates(ledger)
    refs = [item["ref"] for item in candidates]
    # 同一 ref 只留最后一次（更新的说明），并按最后一次出现的阶段排序。
    assert refs == ["task:aaaa1111", str(live)]
    assert candidates[1]["note"] == "报告（更新说明）"
    assert candidates[1]["stage_index"] == 2
    assert [item["candidate_id"] for item in candidates] == [1, 2]
    # 显式限定候选集时，不在集合内的阶段不产出候选；已收口阶段同样排除。
    assert stage_ref_candidates(ledger, stage_ids={"frontdoor-stage-3"}) == []


def test_selection_is_parsed_and_backfilled_verbatim(tmp_path: Path) -> None:
    live = tmp_path / "keep.txt"
    live.write_text("x", encoding="utf-8")
    dead = tmp_path / "gone.txt"
    candidates = [
        {"candidate_id": 1, "ref": str(dead), "note": "已失效产物", "stage_index": 1, "stage_id": "s1"},
        {"candidate_id": 2, "ref": str(live), "note": "有效产物", "stage_index": 1, "stage_id": "s1"},
        {"candidate_id": 3, "ref": "task:7e2a270eec34", "note": "在跑的任务", "stage_index": 2, "stage_id": "s2"},
    ]
    text, selected = CreateAgentCeoFrontDoorRunner._frontdoor_split_stage_ref_selection(
        "## 一、身份\n管家角色。\n\n## 证据索引\n- [#2]\n- [#3]\n- [#99]\n- 2\n\n## 二、待办\n继续跑任务"
    )
    assert selected == [2, 3, 99]
    assert "证据索引" not in text
    assert "## 二、待办" in text

    section, count, dropped = CreateAgentCeoFrontDoorRunner._frontdoor_render_stage_ref_index(candidates, selected)
    lines = section.splitlines()
    assert lines[0] == ops.FRONTDOOR_STAGE_REF_INDEX_HEADING
    # 逐字回填：ref 与 note 都取候选原文，越界编号丢弃。
    assert f"- stage 1 | {live} — 有效产物" in lines
    assert "- stage 2 | task:7e2a270eec34 — 在跑的任务" in lines
    assert count == 2
    assert dropped == 0

    # 死链只按文件系统判定：task: / artifact: 句柄不能被误杀成死链。
    _, alive_count, dropped_dead = CreateAgentCeoFrontDoorRunner._frontdoor_render_stage_ref_index(
        candidates, [1, 2, 3]
    )
    assert alive_count == 2
    assert dropped_dead == 1
    assert CreateAgentCeoFrontDoorRunner._frontdoor_is_filesystem_ref("task:7e2a270eec34") is False
    assert CreateAgentCeoFrontDoorRunner._frontdoor_is_filesystem_ref("c601b416") is False
    assert CreateAgentCeoFrontDoorRunner._frontdoor_is_filesystem_ref(str(live)) is True


def test_missing_index_section_yields_no_selection() -> None:
    text, selected = CreateAgentCeoFrontDoorRunner._frontdoor_split_stage_ref_selection("只有正文，没选引用。")
    assert (text, selected) == ("只有正文，没选引用。", [])
    section, count, dropped = CreateAgentCeoFrontDoorRunner._frontdoor_render_stage_ref_index([], [])
    assert (section, count, dropped) == ("", 0, 0)


# ---- 尾部边界（raw 窗口穿过压缩）-------------------------------------------


def test_summarized_stage_ids_excludes_window_active_and_tail_stages() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    ledger = _ledger(
        [
            _stage(1, tool_call_ids=["c1"]),
            _stage(2, tool_call_ids=["c2"]),
            _stage(3, tool_call_ids=["c3"]),
            _stage(4, tool_call_ids=["c4"]),
            _stage(5, tool_call_ids=["c5"]),
            _stage(6, visible=False),
        ]
    )
    ledger["active_stage_id"] = "frontdoor-stage-7"
    ledger["stages"].append({**_stage(7), "status": "active"})
    tail = [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "function": {"name": "exec"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "结果"},
    ]
    hidden = runner._frontdoor_summarized_stage_ids(stage_state=ledger, recent_tail=tail)
    # 1 = 被摘要；2 = 肉身还在尾部，收了就是黑洞；3/4/5 = 保留 raw 窗口；6 = 已收口；7 = 活动。
    assert hidden == ["frontdoor-stage-1"]


def test_compaction_tail_count_covers_raw_window_and_respects_cap() -> None:
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace())
    ledger = _ledger([_stage(1), _stage(2), _stage(3, tool_call_ids=["c3"])])

    body = [{"role": "system", "content": "基础提示"}]
    body.append({"role": "assistant", "content": "", "tool_calls": [{"id": "c3", "function": {"name": "exec"}}]})
    for position in range(10):
        body.append({"role": "user" if position % 2 == 0 else "assistant", "content": f"对话 {position}"})
    # raw 窗口（stage 3）的肉身必须活着穿过压缩：尾部从它的第一条消息开始。
    assert runner._frontdoor_compaction_tail_count(body, stage_state=ledger) == len(body) - 1

    # 窗口跨度过大时退回按条数保留：尾部是压缩后请求体的不可压缩部分，放太大就没压缩了。
    wide = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c3", "function": {"name": "exec"}}]}]
    for position in range(60):
        wide.append({"role": "user" if position % 2 == 0 else "assistant", "content": f"对话 {position}"})
    assert runner._frontdoor_compaction_tail_count(wide, stage_state=ledger) == 4
    assert runner._frontdoor_compaction_tail_count(body, stage_state=_ledger([])) == 4


# ---- 压缩链路端到端 ---------------------------------------------------------


def _run_compression(
    monkeypatch,
    tmp_path: Path,
    *,
    helper_text: str,
    archive_fails: bool = False,
):
    live = tmp_path / "keep.txt"
    live.write_text("x", encoding="utf-8")
    stages = [
        _stage(1, key_refs=[{"ref": str(live), "note": "有效产物"}, {"ref": str(tmp_path / "gone.txt"), "note": "已失效"}]),
        _stage(2, key_refs=[{"ref": "task:7e2a270eec34", "note": "在跑的任务"}]),
        _stage(3),
        _stage(4),
        _stage(5),
    ]
    session = SimpleNamespace(
        _frontdoor_canonical_context=_ledger(stages),
        _frontdoor_stage_state=_ledger([]),
    )
    runner = CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(workspace=str(tmp_path)))
    captured: dict[str, object] = {}

    async def _fake_helper(**kwargs):
        captured["messages"] = list(kwargs["messages"])
        return helper_text, None

    async def _fake_snapshot(**kwargs):
        return None

    monkeypatch.setattr(runner, "_run_frontdoor_compression_helper_request", _fake_helper)
    monkeypatch.setattr(runner, "_emit_frontdoor_runtime_snapshot", _fake_snapshot)
    monkeypatch.setattr(runner, "_resolve_frontdoor_send_model_context_window", lambda **_: {"context_window_tokens": 200_000})
    monkeypatch.setattr(runner, "_estimate_frontdoor_send_total_tokens", lambda **_: 1_000)
    monkeypatch.setattr(runner, "_build_frontdoor_provider_request_body_preview", lambda **kwargs: {})
    if archive_fails:

        def _failing_temp_dir(session_key):
            raise OSError("disk full")

        monkeypatch.setattr(runner, "_ceo_session_temp_dir", _failing_temp_dir)

    result = asyncio.run(
        runner._run_frontdoor_llm_token_compression(
            state={"session_key": "web:shared", "prompt_cache_key": "", "parallel_enabled": False},
            runtime=SimpleNamespace(context=SimpleNamespace(session=session)),
            request_messages=_prompt_messages(blocks_for=[1, 2, 3, 4, 5]),
            model_refs=["openai:gpt-5.2"],
            tool_schemas=[],
        )
    )
    return result, session, captured, live


def test_token_compression_backfills_selected_refs_and_archives_ledger(monkeypatch, tmp_path: Path) -> None:
    result, session, captured, live = _run_compression(
        monkeypatch,
        tmp_path,
        helper_text="## 一、身份\n管家角色。\n\n## 证据索引\n- [#1]\n- [#2]\n- [#3]\n- [#9]\n",
    )
    assert result.history_shrink_reason == "token_compression"
    instruction = str(captured["messages"][-1]["content"])
    assert ops._FRONTDOOR_STAGE_REF_CANDIDATE_HEADING in instruction
    assert "[#1]" in instruction and "[#2]" in instruction

    summary = next(
        str(item.get("content") or "")
        for item in result.request_messages
        if str(item.get("content") or "").startswith("[G3KU_TOKEN_COMPACT_V2]")
    )
    assert "## 一、身份" in summary
    # 模型只回编号，正文由运行时逐字回填；死链 (#2) 与越界编号 (#9) 都不出现。
    assert f"- stage 1 | {live} — 有效产物" in summary
    assert "gone.txt" not in summary
    assert "- stage 2 | task:7e2a270eec34 — 在跑的任务" in summary
    assert ops.FRONTDOOR_STAGE_ARCHIVE_HEADING in summary

    payload = json.loads(summary.splitlines()[1])
    archive_ref = payload["stage_archive"]["ref"]
    archived = json.loads(Path(archive_ref).read_text(encoding="utf-8"))
    assert archived["stage_count"] == 2
    # 归档保留逐字 key_refs 全量（含死链），收口不等于丢数据。
    assert {item["ref"].split("/")[-1].split("\\")[-1] for stage in archived["stages"] for item in stage["key_refs"]} == {
        "keep.txt",
        "gone.txt",
        "task:7e2a270eec34",
    }

    marked = {stage["stage_id"]: stage.get("context_visible") for stage in session._frontdoor_canonical_context["stages"]}
    # 压缩本身不翻账本标记：发送失败或压缩后被暂停时基线不会推进，标记必须留到那一步。
    assert all(value is None for value in marked.values())
    assert payload["stage_archive"]["stage_ids"] == ["frontdoor-stage-1", "frontdoor-stage-2"]

    pending = CreateAgentCeoFrontDoorRunner._frontdoor_stage_archive_ids(result.request_messages)
    assert CreateAgentCeoFrontDoorRunner._frontdoor_hide_summarized_stages(session, pending) == 2
    marked = {stage["stage_id"]: stage.get("context_visible") for stage in session._frontdoor_canonical_context["stages"]}
    assert marked["frontdoor-stage-1"] is False
    assert marked["frontdoor-stage-2"] is False
    # 保留 raw 窗口的阶段不收口，压缩后仍需它承载近场执行细节。
    assert marked["frontdoor-stage-3"] is None
    assert marked["frontdoor-stage-5"] is None
    # 同一份基线被重复提交（每个请求都会再持久化一次）不得二次改动账本。
    assert CreateAgentCeoFrontDoorRunner._frontdoor_hide_summarized_stages(session, pending) == 0

    assert result.diagnostics["stage_ref_selected_count"] == 2
    assert result.diagnostics["stage_ref_dropped_dead"] == 1
    assert result.diagnostics["stage_archive_pending_count"] == 2
    assert result.diagnostics["stage_ref_candidate_count"] == 3


def test_token_compression_without_selection_still_archives(monkeypatch, tmp_path: Path) -> None:
    result, session, _captured, _live = _run_compression(monkeypatch, tmp_path, helper_text="## 一、身份\n只有正文。")
    summary = next(
        str(item.get("content") or "")
        for item in result.request_messages
        if str(item.get("content") or "").startswith("[G3KU_TOKEN_COMPACT_V2]")
    )
    # 模型不选引用不是错误：不出现索引小节，收口与归档照常，绝不回退成逐轮全量渲染。
    assert ops.FRONTDOOR_STAGE_REF_INDEX_HEADING not in summary
    assert ops.FRONTDOOR_STAGE_ARCHIVE_HEADING in summary
    assert result.diagnostics["stage_ref_selected_count"] == 0
    assert result.diagnostics["stage_archive_pending_count"] == 2
    assert session._frontdoor_canonical_context["stages"][0].get("context_visible") is None
    assert CreateAgentCeoFrontDoorRunner._frontdoor_stage_archive_ids(result.request_messages) == [
        "frontdoor-stage-1",
        "frontdoor-stage-2",
    ]


def test_token_compression_is_idempotent_across_repeats(monkeypatch, tmp_path: Path) -> None:
    """同一份账本重复压缩：候选编号口径与收口清单条数必须一致。"""
    first, _session, _captured, _live = _run_compression(monkeypatch, tmp_path, helper_text="## 一、身份\n第一版摘要。")
    assert first.diagnostics["stage_archive_pending_count"] == 2
    second, _session2, captured, _live = _run_compression(monkeypatch, tmp_path, helper_text="## 一、身份\n第二版摘要。")
    # _run_compression 每次重建账本，因此这里断言的是同一份候选集的稳定编号口径。
    assert "[#1]" in str(captured["messages"][-1]["content"])
    assert second.diagnostics["stage_ref_candidate_count"] == first.diagnostics["stage_ref_candidate_count"]
    assert second.diagnostics["stage_archive_pending_count"] == first.diagnostics["stage_archive_pending_count"]


def test_token_compression_skips_archive_when_export_fails(monkeypatch, tmp_path: Path) -> None:
    """归档落不了盘就整轮不收口：宁可不缩，也不能把阶段收进模型打不开的地方。"""
    result, session, _captured, _live = _run_compression(
        monkeypatch,
        tmp_path,
        helper_text="## 一、身份\n摘要正文。",
        archive_fails=True,
    )
    summary = next(
        str(item.get("content") or "")
        for item in result.request_messages
        if str(item.get("content") or "").startswith("[G3KU_TOKEN_COMPACT_V2]")
    )
    assert ops.FRONTDOOR_STAGE_ARCHIVE_HEADING not in summary
    assert json.loads(summary.splitlines()[1]).get("stage_archive") is None
    assert result.diagnostics["stage_archive_ref"] == ""
    assert CreateAgentCeoFrontDoorRunner._frontdoor_stage_archive_ids(result.request_messages) == []
    assert all(stage.get("context_visible") is None for stage in session._frontdoor_canonical_context["stages"])


def test_hide_marks_across_disjoint_stage_id_namespaces() -> None:
    """live 复现：canonical 链与本轮 stage_state 各自维护一套 stage_id（实测交集 0）。

    只按 id 标记会漏掉 stage_state，而合并去重留下的正是较新的那份副本——等于完全没收口。"""
    early = {**_stage(1), "stage_id": "frontdoor-stage-1", "created_at": "2026-09-19T20:00:00+08:00"}
    late_copy = {**early, "stage_id": "frontdoor-stage-843", "stage_index": 843}
    session = SimpleNamespace(
        _frontdoor_canonical_context=_ledger([early]),
        _frontdoor_stage_state=_ledger([late_copy]),
    )
    marked = CreateAgentCeoFrontDoorRunner._frontdoor_hide_summarized_stages(session, ["frontdoor-stage-1"])
    assert marked == 2
    assert session._frontdoor_canonical_context["stages"][0]["context_visible"] is False
    assert session._frontdoor_stage_state["stages"][0]["context_visible"] is False


def test_combine_propagates_archive_flag_onto_surviving_copy() -> None:
    """合并视图去重后必须仍带着收口标记，否则块会在下一轮整批长回来。"""
    durable = _ledger([_stage(1, visible=False)])
    turn_copy = {**_stage(1), "stage_id": "frontdoor-stage-900", "stage_index": 900}
    combined = combine_canonical_context(durable, _ledger([turn_copy]))
    assert len(combined["stages"]) == 1
    assert combined["stages"][0]["context_visible"] is False
    assert completed_stage_blocks(combined) == []

    # 同一 stage_id 的重复副本同理：被丢弃副本上的标记要转移到存活副本。
    by_id = normalize_frontdoor_canonical_context(
        _ledger([_stage(2, visible=False), {**_stage(2), "completed_stage_summary": "结论 2 更新"}])
    )
    assert by_id["stages"][0]["context_visible"] is False


def test_live_shaped_ledger_renders_only_the_raw_window_after_archive() -> None:
    """ext:qq-official 会话的真实形态：canonical 1..382（372 已收口）+ stage_state 843..1226。"""
    canonical_stages = [
        _stage(index, visible=index > 372) for index in range(1, 383)
    ]
    turn_stages = [
        {**stage, "stage_id": f"frontdoor-stage-{842 + int(stage['stage_index'])}", "stage_index": 842 + int(stage["stage_index"])}
        for stage in canonical_stages
    ]
    turn_stages.append(_stage(383, visible=True) | {"stage_id": "frontdoor-stage-1225", "stage_index": 1225})
    turn_stages.append(_stage(384, visible=True) | {"stage_id": "frontdoor-stage-1226", "stage_index": 1226})
    combined = combine_canonical_context(_ledger(canonical_stages), _ledger(turn_stages))
    blocks = completed_stage_blocks(combined, skip_stage_ids=retained_completed_stage_ids(combined, keep_latest=3))
    assert len(blocks) < 10, f"收口后块数应塌到个位数，实际 {len(blocks)}"


def test_stage_archive_applies_at_ledger_commit_not_session_attribute() -> None:
    """回归：收口标记必须在账本提交点应用。

    实盘失败形态：标记打在会话属性上，回合收尾用轮初 state 快照重建 canonical 并回灌，
    标记在同回合内被整体覆盖 → marked=0、阶段块照旧逐轮渲染。这里用"轮初快照无标记"
    的 result 复现该覆盖，断言应用点产出的账本仍带标记。"""
    archived = [stage["stage_id"] for stage in (_stage(index) for index in range(1, 6))]
    body = [
        {"role": "system", "content": "基础提示"},
        {
            "role": "assistant",
            "content": "[G3KU_TOKEN_COMPACT_V2]\n"
            + json.dumps(
                {
                    "kind": "frontdoor_token_compaction_llm",
                    "history_message_count": 40,
                    "stage_archive": {"ref": "x", "stage_ids": archived},
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n\n摘要正文",
        },
    ]
    # finalize 产出的两份账本都来自轮初快照：还没有任何标记。
    result = {
        "frontdoor_canonical_context": _ledger([_stage(index) for index in range(1, 6)]),
        "frontdoor_stage_state": _ledger([{**_stage(index), "stage_id": f"turn-{index}"} for index in range(1, 6)]),
    }
    applied = CreateAgentCeoFrontDoorRunner._frontdoor_apply_stage_archive(result, body)
    assert applied == 10  # 两份存储各 5 条（第二套 stage_id 靠内容身份跨存储命中）
    assert all(stage.get("context_visible") is False for stage in result["frontdoor_canonical_context"]["stages"])
    assert all(stage.get("context_visible") is False for stage in result["frontdoor_stage_state"]["stages"])
    # 幂等：同一份基线被再次提交不得二次改动
    assert CreateAgentCeoFrontDoorRunner._frontdoor_apply_stage_archive(result, body) == 0
    # 没有收口清单的基线不动账本
    untouched = {"frontdoor_canonical_context": _ledger([_stage(9)]), "frontdoor_stage_state": _ledger([])}
    assert CreateAgentCeoFrontDoorRunner._frontdoor_apply_stage_archive(untouched, [{"role": "user", "content": "hi"}]) == 0


def test_hide_helper_marks_both_durable_stores_and_skips_active() -> None:
    session = SimpleNamespace(
        _frontdoor_canonical_context=_ledger([_stage(1), _stage(2)]),
        _frontdoor_stage_state=_ledger([{**_stage(1, ), "status": "active"}]),
    )
    marked = CreateAgentCeoFrontDoorRunner._frontdoor_hide_summarized_stages(session, ["frontdoor-stage-1", "frontdoor-stage-2"])
    assert marked == 2
    assert session._frontdoor_canonical_context["stages"][0]["context_visible"] is False
    # 活动阶段一律不收口：它的轮次还在写。
    assert session._frontdoor_stage_state["stages"][0].get("context_visible") is None
    again = CreateAgentCeoFrontDoorRunner._frontdoor_hide_summarized_stages(session, ["frontdoor-stage-1"])
    assert again == 0
