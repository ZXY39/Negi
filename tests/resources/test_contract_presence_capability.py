"""刀一：契约在场即能力 —— 在场判据、撤销回写台账、重复读守卫同判据。

不变量：一个工具在某一跳能不能被调用，取决于它的 toolskill 契约正文在这一跳是不是在场。
判据只有一个函数（`g3ku/runtime/tool_context_presence.py`），两个载体都喂给它：
未被压缩删除的 loader 结果行、阶段台账里的 `kept_tool_contexts` 条目。

覆盖 `docs/FIX_PLAN_contract-presence-capability.md` §4 的 1–6 项（刀一能覆盖的部分）。
撤销**必须回写台账**（不只是收窄视图）：只收窄视图的话，该名字既不在 callable、又被
candidate 的「排除已提升」规则挡在门外，模型 load 它只会拿到 `already_hydrated`，
那是文档禁止的第 4 态。

注：`test_contract_presence_*` 三条只测新模块自己的算术，在未修改的树上同样是绿的，
不算回归覆盖；真正的撤销/在场覆盖从 `test_node_*` 起。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from g3ku.runtime.context.execution_tool_selection import build_execution_tool_selection
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from g3ku.runtime.tool_context_presence import (
    contract_presence,
    contract_presence_index,
    kept_contract_index,
    kept_tool_contexts_from_frames,
    loader_contract_index,
    normalize_kept_tool_contexts,
    partition_contract_presence,
)
from g3ku.runtime.web_ceo_sessions import _normalized_completed_continuity_snapshot
from main.monitoring.log_service import TaskLogService
from main.monitoring.models import TaskProjectionRuntimeFrameRecord
from main.runtime.internal_tools import STAGE_TOOL_NAME
from main.runtime.react_loop import ReActToolLoop
from main.service.runtime_service import MainRuntimeService

TOOL_ID = "filesystem_write"
FINGERPRINT = "sha256:" + "a" * 64
OTHER_ID = "content_open"


def _loader_message(tool_id: str = TOOL_ID, fingerprint: str = FINGERPRINT, *, ok: bool = True) -> dict:
    payload = {"ok": ok, "tool_id": tool_id}
    if ok:
        payload["tool_context_fingerprint"] = fingerprint
    return {"role": "tool", "name": "load_tool_context", "content": json.dumps(payload, ensure_ascii=False)}


def _kept_entry(tool_id: str = TOOL_ID, fingerprint: str = FINGERPRINT) -> dict:
    return {
        "tool_id": tool_id,
        "tool_context_fingerprint": fingerprint,
        "body": "# filesystem_write\n契约正文",
    }


def _kept_stages(tool_id: str = TOOL_ID, fingerprint: str = FINGERPRINT) -> list[dict]:
    return [{"stage_id": "stage-1", "kept_tool_contexts": [_kept_entry(tool_id, fingerprint)]}]


# ---------------------------------------------------------------------------
# §2.1 共享判据：两个载体、失败载荷、skill 不参与
# ---------------------------------------------------------------------------


def test_contract_presence_accepts_both_carriers() -> None:
    present, fingerprint = contract_presence(TOOL_ID, request_messages=[_loader_message()])
    assert present is True
    assert fingerprint == FINGERPRINT

    present, fingerprint = contract_presence(TOOL_ID, kept_stage_contexts=_kept_stages())
    assert present is True
    assert fingerprint == FINGERPRINT

    present, fingerprint = contract_presence(TOOL_ID, request_messages=[], kept_stage_contexts=[])
    assert present is False
    assert fingerprint == ""


def test_contract_presence_index_merges_carriers_and_skips_failures() -> None:
    index = contract_presence_index(
        request_messages=[_loader_message(ok=False), _loader_message(OTHER_ID, "sha256:bbb")],
        kept_stage_contexts=_kept_stages("skill_backed_tool", "sha256:ccc"),
    )
    assert OTHER_ID in index
    assert "skill_backed_tool" in index
    assert TOOL_ID not in index
    assert loader_contract_index([_loader_message(ok=False)]) == {}
    assert kept_contract_index([{"tool_id": "", "tool_context_fingerprint": "x"}]) == {}

    present, absent = partition_contract_presence([OTHER_ID, "skill_backed_tool", "filesystem_delete"], index=index)
    assert present == [OTHER_ID, "skill_backed_tool"]
    assert absent == ["filesystem_delete"]


def test_skill_contexts_do_not_participate_in_presence() -> None:
    """skill 不水合、没有 callable 可摘，所以保留 skill 正文不构成在场证据。"""
    stages = [{"stage_id": "s1", "kept_skill_contexts": [{"skill_id": TOOL_ID, "body": "正文"}]}]
    present, _ = contract_presence(TOOL_ID, kept_stage_contexts=stages)
    assert present is False
    assert kept_tool_contexts_from_frames({"stages": stages}) == []
    assert normalize_kept_tool_contexts([{"tool_id": " x ", "body": "b"}])[0]["tool_id"] == "x"


# ---------------------------------------------------------------------------
# 节点车道：撤销回写台账 + candidate 复位 + 重新 load 恢复
# ---------------------------------------------------------------------------


class _NodeLogService:
    """带真实归一化白名单的假账本：字段漏白名单必须能被这些用例抓到。"""

    def __init__(self) -> None:
        self._frames: dict[tuple[str, str], dict] = {}

    def upsert_frame(self, task_id: str, payload: dict, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        node_id = str((payload or {}).get("node_id") or "").strip()
        self._frames[(str(task_id), node_id)] = TaskLogService._sanitize_runtime_frame(dict(payload or {}))

    def update_frame(self, task_id: str, node_id: str, mutate, publish_snapshot: bool = True) -> None:
        _ = publish_snapshot
        key = (str(task_id), str(node_id))
        current = self._frames.get(key) or {"node_id": node_id}
        mutated = mutate(TaskLogService._sanitize_runtime_frame(dict(current)))
        self._frames[key] = TaskLogService._sanitize_runtime_frame(dict(mutated or {}))

    def read_runtime_frame(self, task_id: str, node_id: str) -> dict:
        stored = self._frames.get((str(task_id), str(node_id)))
        if stored is None:
            return {}
        record = TaskProjectionRuntimeFrameRecord(task_id=str(task_id), node_id=str(node_id), payload=stored)
        return TaskLogService._hydrate_runtime_frame_record(object.__new__(TaskLogService), record)


def _node_service(
    *,
    hydrated: list[str],
    stages: dict | None = None,
    visible: list[str] | None = None,
) -> tuple[MainRuntimeService, _NodeLogService]:
    service = object.__new__(MainRuntimeService)
    log_service = _NodeLogService()
    service.log_service = log_service
    effective_visible = list(
        visible
        if visible is not None
        else [TOOL_ID, OTHER_ID, "filesystem_edit", "exec", STAGE_TOOL_NAME, "submit_final_result"]
    )
    service.list_effective_tool_names = lambda *, actor_role, session_id: list(effective_visible)
    service.list_visible_tool_families = lambda *, actor_role, session_id: [
        SimpleNamespace(tool_id="filesystem", actions=[SimpleNamespace(executor_names=[TOOL_ID, "filesystem_edit"])]),
        SimpleNamespace(tool_id="content", actions=[SimpleNamespace(executor_names=[OTHER_ID])]),
    ]
    log_service.upsert_frame(
        "task-cp",
        {
            "node_id": "node-cp",
            "node_kind": "execution",
            "hydrated_executor_state": list(hydrated),
            "hydrated_executor_names": list(hydrated),
            "execution_stages": dict(stages or {}),
            "messages": [],
        },
    )
    return service, log_service


def _node_obj() -> SimpleNamespace:
    return SimpleNamespace(node_id="node-cp", task_id="task-cp", node_kind="execution", can_spawn_children=False)


def _task_obj() -> SimpleNamespace:
    return SimpleNamespace(task_id="task-cp", session_id="web:shared", metadata={})


def test_node_contract_absent_hydration_is_removed_from_ledger() -> None:
    """§4.1 契约被裁撤 + 未 keep ⇒ 不在 callable，且**台账**被回写。"""
    service, log_service = _node_service(hydrated=[TOOL_ID])
    present = service._node_hydrated_executor_names(
        task_id="task-cp",
        node_id="node-cp",
        actor_role="execution",
        session_id="web:shared",
        request_messages=[],
    )
    assert present == []

    frame = log_service.read_runtime_frame("task-cp", "node-cp")
    assert TOOL_ID not in list(frame["hydrated_executor_state"] or [])
    assert frame["hydration_revoked_executor_names"] == [TOOL_ID]


def test_node_contract_present_hydration_stays_callable() -> None:
    service, log_service = _node_service(hydrated=[TOOL_ID])
    present = service._node_hydrated_executor_names(
        task_id="task-cp",
        node_id="node-cp",
        actor_role="execution",
        session_id="web:shared",
        request_messages=[_loader_message()],
    )
    assert present == [TOOL_ID]
    frame = log_service.read_runtime_frame("task-cp", "node-cp")
    assert frame["hydration_revoked_executor_names"] == []


def test_node_kept_stage_context_keeps_tool_callable() -> None:
    """§4.2 合成一条保留契约条目（刀二才写真身）⇒ 仍在 callable、不撤销。"""
    service, log_service = _node_service(hydrated=[TOOL_ID])
    present = service._node_hydrated_executor_names(
        task_id="task-cp",
        node_id="node-cp",
        actor_role="execution",
        session_id="web:shared",
        request_messages=[],
        kept_stage_contexts=_kept_stages(),
    )
    assert present == [TOOL_ID]
    frame = log_service.read_runtime_frame("task-cp", "node-cp")
    assert frame["hydration_revoked_executor_names"] == []
    assert frame["hydrated_executor_state"] == [TOOL_ID]


def test_node_kept_context_is_read_from_stage_ledger() -> None:
    """阶段账本的家在 `node.metadata['execution_stages']`，callable 组装必须从那里读保留正文。"""
    service, _log = _node_service(hydrated=[TOOL_ID])
    node = _node_obj()
    node.metadata = {"execution_stages": {"stages": _kept_stages()}}
    callable_names = service._callable_tool_names_for_node(
        task=_task_obj(),
        node=node,
        request_messages=[],
    )
    assert TOOL_ID in callable_names


def test_node_revoked_name_returns_to_candidate() -> None:
    """§0.3 撤销后名字自动回到 candidate：candidate = 治理可见 −（callable ∪ 已提升）。"""
    service, _log = _node_service(hydrated=[TOOL_ID])
    node, task = _node_obj(), _task_obj()
    assert TOOL_ID in service._callable_tool_names_for_node(task=task, node=node)

    callable_names = service._callable_tool_names_for_node(task=task, node=node, request_messages=[])
    assert TOOL_ID not in callable_names

    visible = service.list_effective_tool_names(actor_role="execution", session_id="web:shared")
    selection = build_execution_tool_selection(
        prompt="p",
        goal="g",
        core_requirement="g",
        visible_tool_families=[],
        visible_tool_names=list(visible),
        always_callable_tool_names=[name for name in visible if name != TOOL_ID],
        promoted_tool_names=[],
        schema_size_by_executor={name: 100 for name in visible},
    )
    assert TOOL_ID in list(selection.candidate_tool_names or [])
    assert TOOL_ID not in list(selection.hydrated_tool_names or [])


def test_node_reload_clears_revocation_and_restores_promotion() -> None:
    """§4.1 后半：重新 load 后恢复提升。"""
    service, log_service = _node_service(hydrated=[TOOL_ID])
    service._node_hydrated_executor_names(
        task_id="task-cp",
        node_id="node-cp",
        actor_role="execution",
        session_id="web:shared",
        request_messages=[],
    )
    frame = log_service.read_runtime_frame("task-cp", "node-cp")
    assert TOOL_ID in frame["hydration_revoked_executor_names"]
    assert TOOL_ID not in frame["hydrated_executor_state"]

    service._promote_tool_context_hydration(
        task_id="task-cp",
        node_id="node-cp",
        tool_call=SimpleNamespace(name="load_tool_context", arguments={"tool_id": TOOL_ID}),
        raw_result={"ok": True, "tool_id": TOOL_ID, "hydration_targets": [TOOL_ID]},
        runtime_context={
            "session_key": "web:shared",
            "actor_role": "execution",
            "candidate_tool_names": [TOOL_ID],
        },
    )
    frame = log_service.read_runtime_frame("task-cp", "node-cp")
    assert frame["hydrated_executor_state"] == [TOOL_ID]
    assert TOOL_ID not in list(frame["hydration_revoked_executor_names"] or [])


def test_node_revocation_does_not_touch_lru_eviction_field() -> None:
    """§4.5 LRU 淘汰与契约撤销是两种原因，混在一起下次就分不出账。"""
    service, log_service = _node_service(hydrated=[TOOL_ID])
    log_service.update_frame(
        "task-cp",
        "node-cp",
        lambda frame: {**frame, "hydration_evicted_executor_names": [OTHER_ID]},
    )
    service._node_hydrated_executor_names(
        task_id="task-cp",
        node_id="node-cp",
        actor_role="execution",
        session_id="web:shared",
        request_messages=[],
    )
    frame = log_service.read_runtime_frame("task-cp", "node-cp")
    assert frame["hydration_revoked_executor_names"] == [TOOL_ID]
    assert list(frame["hydration_evicted_executor_names"] or []) == [OTHER_ID]


def test_revocation_field_survives_node_frame_normalization() -> None:
    """§4.6 白名单漏一份 = 逐轮被抹掉，可观测症状是"撤销没生效"。"""
    payload = TaskLogService._sanitize_runtime_frame(
        {
            "node_id": "n",
            "hydrated_executor_state": ["a"],
            "hydration_revoked_executor_names": ["b"],
        }
    )
    assert payload["hydration_revoked_executor_names"] == ["b"]

    record = TaskProjectionRuntimeFrameRecord(task_id="t", node_id="n", payload=payload)
    hydrated = TaskLogService._hydrate_runtime_frame_record(object.__new__(TaskLogService), record)
    assert hydrated["hydration_revoked_executor_names"] == ["b"]

    snapshot = TaskLogService._sanitize_callable_tool_snapshot(
        {"hydrated_executor_state": ["a"], "hydration_revoked_executor_names": ["b"]}
    )
    assert snapshot["hydration_revoked_executor_names"] == ["b"]

    default_frame = TaskLogService._default_frame(node_id="n")
    assert default_frame["hydration_revoked_executor_names"] == []


# ---------------------------------------------------------------------------
# 前门车道：三个算点必须给出同一份 callable
# ---------------------------------------------------------------------------


def _frontdoor_state(*, hydrated: list[str], messages: list[dict], stages: list[dict] | None = None) -> dict:
    state = initial_persistent_state(user_input={"content": "q"})
    state["tool_names"] = ["exec", *hydrated]
    state["hydrated_tool_names"] = list(hydrated)
    state["provider_tool_names"] = ["exec", *hydrated]
    state["candidate_tool_names"] = []
    state["rbac_visible_tool_names"] = ["exec", *hydrated]
    state["messages"] = list(messages)
    # 真实图状态始终带 model_refs（`state_models.CeoPersistentState.model_refs`），
    # `_refresh_prompt_cache_state` 只在状态缺它时才回落到读磁盘配置取模型。测试里没有
    # `.g3ku/config.json`，不钉住这一份就会在算点二上 FileNotFoundError——那是取模型，
    # 不是在场判据，别让环境问题冒充判据失败。
    state["model_refs"] = ["contract-presence-test-model"]
    state["frontdoor_stage_state"] = {
        "active_stage_id": "s1",
        "stages": [{"stage_id": "s1", "status": "active", **({"kept_tool_contexts": []} if stages is None else {})}],
    }
    if stages:
        state["frontdoor_stage_state"]["stages"].extend(stages)
    return state


def _ops() -> CeoFrontDoorRuntimeOps:
    ops = CeoFrontDoorRuntimeOps.__new__(CeoFrontDoorRuntimeOps)
    ops._loop = SimpleNamespace(tools={}, app_config=None, main_task_service=None)
    return ops


def test_frontdoor_callable_drops_contract_absent_hydration() -> None:
    ops = _ops()
    state = _frontdoor_state(hydrated=[TOOL_ID], messages=[])
    assert TOOL_ID not in ops._frontdoor_callable_tool_names_for_state(state)

    present_state = _frontdoor_state(hydrated=[TOOL_ID], messages=[_loader_message()])
    assert TOOL_ID in ops._frontdoor_callable_tool_names_for_state(present_state)


def test_frontdoor_kept_context_counts_as_present() -> None:
    ops = _ops()
    state = _frontdoor_state(hydrated=[TOOL_ID], messages=[], stages=_kept_stages())
    assert TOOL_ID in ops._frontdoor_callable_tool_names_for_state(state)
    # 只断言"仍在 callable"在未修改的树上是假绿（今日什么都不滤，它当然在）。加上判据侧的
    # 断言：在场的是**保留正文**这个载体，名字既不 revoked 也留在台账里，才证明是它撑住的。
    kept, revoked = ops._frontdoor_contract_presence_partition(state, list(state["hydrated_tool_names"]))
    assert kept == [TOOL_ID]
    assert revoked == []


def test_frontdoor_partition_reports_revoked_names() -> None:
    ops = _ops()
    state = _frontdoor_state(hydrated=[TOOL_ID], messages=[])
    kept, revoked = ops._frontdoor_contract_presence_partition(state, list(state["hydrated_tool_names"]))
    assert kept == []
    assert revoked == [TOOL_ID]


def test_frontdoor_three_compute_points_agree() -> None:
    """§4.3 合同分裂回归：装配路 / 缓存刷新 / 发送预检三处必须同判据。

    只补不删的 provider `tools[]` 不在本判据范围内，因此这里比对的是模型面 callable。
    """
    ops = _ops()
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=None))
    state = _frontdoor_state(hydrated=[TOOL_ID], messages=[])

    # 算点一：装配路 —— message_builder 用经判据筛过的 hydrated 组 callable。
    kept_hydrated, _revoked = ops._frontdoor_contract_presence_partition(state, list(state["hydrated_tool_names"]))
    assembly_names = CeoMessageBuilder._callable_tool_names(
        visible_tool_names=list(state["rbac_visible_tool_names"]),
        hydrated_tool_names=list(kept_hydrated),
    )

    # 算点二：缓存刷新（动态尾部合同的 callable 行由同一个 helper 得出）。
    refreshed = runner._refresh_prompt_cache_state(state=dict(state))
    refresh_names = runner._frontdoor_callable_tool_names_for_state(refreshed)

    # 算点三：发送预检。
    preflight_names = ops._frontdoor_callable_tool_names_for_state(dict(state))

    assert TOOL_ID not in assembly_names
    assert TOOL_ID not in refresh_names
    assert TOOL_ID not in preflight_names
    assert set(refresh_names) == set(preflight_names)


def test_frontdoor_internal_rounds_apply_same_filter() -> None:
    """§4.4 内部轮继承的 callable 也必须过同一在场判据。"""
    ops = _ops()
    for flag in ("heartbeat_internal", "cron_internal"):
        state = _frontdoor_state(hydrated=[TOOL_ID], messages=[])
        state[flag] = True
        assert TOOL_ID not in ops._frontdoor_callable_tool_names_for_state(state), flag
        present = _frontdoor_state(hydrated=[TOOL_ID], messages=[_loader_message()])
        present[flag] = True
        assert TOOL_ID in ops._frontdoor_callable_tool_names_for_state(present), flag


def test_frontdoor_after_tool_results_writes_revoked_into_state() -> None:
    ops = _ops()
    state = _frontdoor_state(hydrated=[TOOL_ID], messages=[])
    state["candidate_tool_names"] = []
    state["candidate_tool_items"] = []
    after = ops._frontdoor_tool_state_after_tool_results(state=state, tool_results=[])
    assert TOOL_ID not in list(after["hydrated_tool_names"] or [])
    assert after["hydration_revoked_executor_names"] == [TOOL_ID]
    assert TOOL_ID not in list(after["tool_names"] or [])


def test_frontdoor_hydration_after_successful_load_is_not_revoked() -> None:
    """本轮刚 load 成功的名字其契约就在同批结果里，不得被当场撤销。"""
    ops = _ops()
    state = _frontdoor_state(hydrated=[], messages=[_loader_message()])
    state["candidate_tool_names"] = [TOOL_ID]
    # candidate 是治理可见集减出来的（FIX_PLAN §0.3「candidate = 治理可见 −（callable ∪ 已提升)」），
    # 所以候选里的名字必然也在 `rbac_visible_tool_names`。helper 按 hydrated 拼可见集，
    # 这条用例 hydrated 为空就漏了它——水合 LRU 会先把不在可见集里的名字滤掉（既有口径），
    # 那是夹具自相矛盾，不是判据把刚 load 的名字撤了。
    state["rbac_visible_tool_names"] = ["exec", TOOL_ID]
    after = ops._frontdoor_tool_state_after_tool_results(
        state=state,
        tool_results=[
            {
                "tool_name": "load_tool_context",
                "raw_result": {
                    "ok": True,
                    "tool_id": TOOL_ID,
                    "hydration_targets": [TOOL_ID],
                    "tool_context_fingerprint": FINGERPRINT,
                },
            }
        ],
    )
    assert after["hydrated_tool_names"] == [TOOL_ID]
    assert after["hydration_revoked_executor_names"] == []


def test_frontdoor_revoked_field_survives_session_normalization() -> None:
    """§4.6 前门两份归一化白名单都要留位。"""
    snapshot = _normalized_completed_continuity_snapshot(
        {"hydrated_tool_names": ["a"], "hydration_revoked_executor_names": ["b"], "updated_at": "t"}
    )
    assert snapshot["hydration_revoked_executor_names"] == ["b"]

    state = initial_persistent_state(user_input={"content": "q"})
    assert state["hydration_revoked_executor_names"] == []


def test_frontdoor_stage_snapshot_carries_kept_tool_contexts() -> None:
    ops = _ops()
    snapshot = ops._frontdoor_stage_state_snapshot({"frontdoor_stage_state": {"stages": _kept_stages()}})
    stage = snapshot["stages"][-1]
    assert stage["kept_tool_contexts"][0]["tool_id"] == TOOL_ID


def test_canonical_context_carries_kept_tool_contexts() -> None:
    from g3ku.runtime.frontdoor.canonical_context import normalize_frontdoor_canonical_context

    normalized = normalize_frontdoor_canonical_context(
        {"stages": [{"stage_id": "s1", "kept_tool_contexts": [_kept_entry()], "rounds": []}]}
    )
    stages = list(normalized.get("stages") or [])
    assert stages
    assert stages[-1]["kept_tool_contexts"][0]["tool_id"] == TOOL_ID


# ---------------------------------------------------------------------------
# 重复读守卫：块内保留正文算作在场 ⇒ 不许放行重读
# ---------------------------------------------------------------------------


def test_duplicate_guards_count_kept_context_as_present() -> None:
    """§4.2 后半：两条车道的重读守卫共用同一在场判据 ⇒ 块内保留正文算在场。

    算在场才会拒重读；不算在场就放行、同一份正文在上下文里出现两次。
    载体本身（loader 行）也要能被同一 helper 认出来，否则守卫换了判据就失配。
    """
    loader_history = [_loader_message()]
    kept = _kept_stages()

    from_loader = ReActToolLoop._latest_load_tool_context_messages_by_tool_id(loader_history)
    assert TOOL_ID in from_loader
    assert TOOL_ID in CeoFrontDoorRuntimeOps._latest_frontdoor_load_tool_context_messages_by_tool_id(loader_history)

    unrelated = [{"role": "tool", "name": "load_tool_context", "content": json.dumps({"ok": True, "tool_id": "z"})}]
    # 对照条：只交 loader 载体、不交保留正文时守卫认不出 TOOL_ID（这条 loader 行连
    # fingerprint 都没有，本就不算契约载体）。带 `kept_stage_contexts` 的那两条断言在下面
    # §4.2 里要求 TOOL_ID **在场**，与此处 `== {}` 不能同时成立，故对照只跑无保留正文的一侧。
    assert ReActToolLoop._latest_load_tool_context_messages_by_tool_id(unrelated) == {}
    assert CeoFrontDoorRuntimeOps._latest_frontdoor_load_tool_context_messages_by_tool_id(unrelated) == {}

    node_from_kept = ReActToolLoop._latest_load_tool_context_messages_by_tool_id(unrelated, kept_stage_contexts=kept)
    assert TOOL_ID in node_from_kept
    assert node_from_kept[TOOL_ID]["name"] == "load_tool_context"
    frontdoor_from_kept = CeoFrontDoorRuntimeOps._latest_frontdoor_load_tool_context_messages_by_tool_id(
        unrelated,
        kept_stage_contexts=kept,
    )
    assert TOOL_ID in frontdoor_from_kept


# ---------------------------------------------------------------------------
# 节点阶段台账 kept_tool_contexts 字段可用
# ---------------------------------------------------------------------------


def test_execution_stage_record_carries_kept_tool_contexts() -> None:
    from main.models import ExecutionStageRecord

    record = ExecutionStageRecord(stage_id="s1", kept_tool_contexts=[_kept_entry()])
    payload = record.model_dump()
    assert payload["kept_tool_contexts"][0]["tool_id"] == TOOL_ID
