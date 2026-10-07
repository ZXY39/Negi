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

from g3ku.runtime.context.execution_tool_selection import build_execution_tool_selection
from g3ku.runtime.frontdoor import _ceo_create_agent_impl as create_agent_impl
from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps
from g3ku.runtime.frontdoor.message_builder import CeoMessageBuilder
from g3ku.runtime.frontdoor.state_models import initial_persistent_state
from g3ku.runtime.kept_contract_snapshot import build_kept_contract_snapshot
from g3ku.runtime.stage_prompt_compaction import KEPT_CONTRACT_HEADING, completed_stage_blocks
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
from main.models import normalize_execution_stage_metadata
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


# ---------------------------------------------------------------------------
# 刀二回归守卫：保留正文是提交点快照，块字节只随账本变
# ---------------------------------------------------------------------------


def test_kept_contract_block_bytes_do_not_follow_resource_file_edits(tmp_path) -> None:
    """裁撤后的块内正文必须**只回放账本**，且仍然撑起 callable 与重复读守卫。

    三条断言各挡一种回归：

    1. 改一次磁盘上的 `toolskills/SKILL.md`，块字节不许变（顺带证"会变"——同一条取正文
       的通道再走一次，正文确实不同了）。阶段块落在历史中段，逐轮重读资源文件会让运营者
       或 `skill-installer` 的一次编辑改动块字节，块之后的整段前缀缓存全断。
    2. 下一跳（请求视图里已经没有 loader 行了）该工具仍在 callable、台账没被撤销——判据
       的第二个载体真被读到了，节点车道走的是 `ExecutionStageState` 对象那份账本。
    3. 重复读守卫同判据：块内正文算在场 ⇒ 再 load 同一 `tool_id` 判成重复读，不会在上下文
       里出现第二份正文。
    """
    toolskill_path = tmp_path / "tools" / TOOL_ID / "toolskills" / "SKILL.md"
    toolskill_path.parent.mkdir(parents=True)
    toolskill_path.write_text(f"# {TOOL_ID}\n\n原始契约正文\n", encoding="utf-8")

    def _read_from_disk(tool_id: str) -> dict:
        return {
            "tool_id": tool_id,
            "content": toolskill_path.read_text(encoding="utf-8"),
            "parameter_contract_markdown": "",
            "required_parameters": [],
            "example_arguments": {},
            "warnings": [],
            "errors": [],
        }

    snapshot = build_kept_contract_snapshot(tool_ids=[TOOL_ID], tool_payload_getter=_read_from_disk)
    assert snapshot["failures"] == []
    entries = snapshot["tool_contexts"]
    assert entries[0]["tool_id"] == TOOL_ID
    assert entries[0]["tool_context_fingerprint"].startswith("tcf:")

    # 提交点落账本后的形态：节点道渲染读的是 pydantic 阶段状态，落盘是它的 JSON 视图，
    # 两份必须是同一份账本（漏一份白名单就等于逐轮被抹掉）。
    stage_state = normalize_execution_stage_metadata(
        {
            "active_stage_id": "",
            "stages": [
                {
                    "stage_id": "s-keep",
                    "stage_index": 1,
                    "stage_kind": "normal",
                    "status": "完成",
                    "stage_goal": "g",
                    "completed_stage_summary": "收尾结论",
                    "context_evicted": True,
                    "kept_tool_contexts": entries,
                    "kept_skill_contexts": [{"skill_id": "demo-skill", "body": "技能正文"}],
                    "rounds": [],
                }
            ],
        }
    )
    ledger = stage_state.model_dump(mode="json")
    rendered = completed_stage_blocks(stage_state)[0]["content"]
    assert KEPT_CONTRACT_HEADING in rendered
    assert "原始契约正文" in rendered
    assert entries[0]["tool_context_fingerprint"] in rendered

    # 运营者改了一次资源文件：正文确实变了（所以逐轮重读会改块字节），块字节却没变。
    toolskill_path.write_text(f"# {TOOL_ID}\n\n改过的契约正文\n", encoding="utf-8")
    assert build_kept_contract_snapshot(tool_ids=[TOOL_ID], tool_payload_getter=_read_from_disk)["tool_contexts"][0][
        "body"
    ] != entries[0]["body"]
    assert completed_stage_blocks(stage_state)[0]["content"] == rendered
    assert completed_stage_blocks(ledger)[0]["content"] == rendered
    assert "改过的契约正文" not in rendered

    # 裁撤后的下一跳：请求视图里再没有 loader 行，撑住 callable 的只有账本那一份正文。
    service, log_service = _node_service(hydrated=[TOOL_ID])
    node = _node_obj()
    node.metadata = {"execution_stages": ledger}
    assert TOOL_ID in service._callable_tool_names_for_node(task=_task_obj(), node=node, request_messages=[])
    assert log_service.read_runtime_frame("task-cp", "node-cp")["hydration_revoked_executor_names"] == []

    # 同一份账本喂给重复读守卫 ⇒ 认得出这一跳已经在场，重读判成重复读、不再产第二份。
    kept_index = ReActToolLoop._latest_load_tool_context_messages_by_tool_id(
        [],
        kept_stage_contexts=kept_tool_contexts_from_frames(ledger),
    )
    assert TOOL_ID in kept_index
    assert kept_index[TOOL_ID]["name"] == "load_tool_context"



def test_revoked_contract_absent_names_return_to_candidate_view() -> None:
    """撤销必须把名字并回候选视图：否则回合内既不可调也不可读（第四态）。"""

    from g3ku.runtime.frontdoor._ceo_runtime_ops import revive_contract_absent_candidates

    revived = revive_contract_absent_candidates(
        candidate_names=["content_open", "cron"],
        revoked_names=["perf_inspect"],
        hydrated_names=[],
        callable_names=["submit_next_stage", "exec"],
        visible_names=["content_open", "cron", "perf_inspect", "exec"],
    )
    assert revived == ["content_open", "cron", "perf_inspect"]

    # 已重新提升的、当前可调的、权限已收回的：都不该并回候选
    for kwargs in (
        {"hydrated_names": ["perf_inspect"]},
        {"callable_names": ["submit_next_stage", "exec", "perf_inspect"]},
        {"visible_names": ["content_open", "cron"]},
    ):
        base = {
            "candidate_names": ["content_open", "cron"],
            "revoked_names": ["perf_inspect"],
            "hydrated_names": [],
            "callable_names": ["submit_next_stage", "exec"],
            "visible_names": ["content_open", "cron", "perf_inspect", "exec"],
        }
        base.update(kwargs)
        assert "perf_inspect" not in revive_contract_absent_candidates(**base)


def test_frontdoor_candidate_view_reads_state_revoked_field() -> None:
    """三个消费点共用的视图方法：读 state 上的撤销记录，不要求调用方自己并集。"""

    from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps

    view = CeoFrontDoorRuntimeOps._frontdoor_candidate_tool_view(
        {
            "candidate_tool_names": ["content_open"],
            "hydration_revoked_executor_names": ["perf_inspect"],
            "hydrated_tool_names": [],
            "tool_names": ["exec", "submit_next_stage"],
            "rbac_visible_tool_names": ["content_open", "perf_inspect", "exec"],
        }
    )
    assert "perf_inspect" in view and "content_open" in view
    assert CeoFrontDoorRuntimeOps._frontdoor_candidate_tool_view(None) == []


def _evicted_stage_state(*, kept_entries: list[dict] | None = None) -> dict:
    stage = {
        "stage_id": "s1",
        "stage_index": 1,
        "stage_kind": "normal",
        "status": "completed",
        "context_evicted": True,
        "rounds": [{"round_id": "r1", "tool_call_ids": ["call-load-1"], "tools": []}],
    }
    if kept_entries:
        stage["kept_tool_contexts"] = list(kept_entries)
    return {"stages": [stage], "active_stage_id": "", "transition_required": False}


def _loader_messages() -> list[dict]:
    return [
        {
            "role": "tool",
            "name": "load_tool_context",
            "tool_call_id": "call-load-1",
            "content": json.dumps(
                {
                    "ok": True,
                    "tool_id": TOOL_ID,
                    "tool_context_fingerprint": "tcf:fixture-presence",
                },
                ensure_ascii=False,
            ),
        }
    ]


def test_partition_treats_evicted_stage_loader_row_as_absent() -> None:
    """判据必须与渲染同源：裁撤阶段的正文行虽在 state['messages'] 里，也算不在场。

    实盘 web:ceo-09057e72cac8 上，裁撤发生在请求体重建时，而判据读裁撤前的视图，
    于是台账不记撤销、候选并回没有输入，尾部契约与候选同时缺这个名字（第四态）。
    """

    ops = _ops()
    state = {"messages": _loader_messages(), "frontdoor_stage_state": _evicted_stage_state()}
    kept, revoked = ops._frontdoor_contract_presence_partition(state, [TOOL_ID])
    assert revoked == [TOOL_ID]
    assert kept == []


def test_partition_kept_context_overrides_stage_eviction() -> None:
    """被 keep_tools 点名的正文仍然在场 ⇒ 不撤销。"""

    ops = _ops()
    state = {
        "messages": _loader_messages(),
        "frontdoor_stage_state": _evicted_stage_state(
            kept_entries=[{
                "tool_id": TOOL_ID,
                "tool_context_fingerprint": "tcf:fixture-presence",
                "body": "# fixture toolskill body",
            }]
        ),
    }
    kept, revoked = ops._frontdoor_contract_presence_partition(state, [TOOL_ID])
    assert kept == [TOOL_ID]
    assert revoked == []


def test_dispatch_excludes_contract_absent_hydrated_tool() -> None:
    """撤销必须落到执行层：只改尾部契约时，那一跳的 callable 已不含它、调用却仍成功。"""

    ops = _ops()
    state = {
        "messages": _loader_messages(),
        "frontdoor_stage_state": _evicted_stage_state(),
        "provider_tool_names": ["exec", TOOL_ID],
        "rbac_visible_tool_names": ["exec", TOOL_ID],
        "tool_names": ["exec", TOOL_ID],
        "hydrated_tool_names": [TOOL_ID],
    }
    names = ops._frontdoor_dispatch_tool_names(state)
    assert TOOL_ID not in names
    assert "exec" in names


def _eviction_batch_state(*, keep_tools: list[str] | None = None) -> dict:
    """一条活动阶段：本阶段 load 过 TOOL_ID，正文行还挂在 `messages` 里，本批要点名裁撤。

    轮次记录带 `arguments.tool_id` 与成功状态：`keep_tools` 的名字只认这份记录，少了它
    keep 那条会被整批拒绝（回执改成"没留"），夹具就测不到留住的那条道。
    """
    stage_arguments = {
        "stage_goal": "run the selected tool calls",
        "tool_round_budget": 4,
        "completed_stage_summary": "已看完仓库结构",
        "drop_completed_stage_tool_detail": True,
    }
    if keep_tools is not None:
        stage_arguments["keep_tools"] = list(keep_tools)
    return {
        "session_key": "web:shared",
        "messages": [
            {"role": "system", "content": "SYSTEM"},
            {"role": "user", "content": "本轮问题"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-load-1",
                        "type": "function",
                        "function": {"name": "load_tool_context", "arguments": json.dumps({"tool_id": TOOL_ID})},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "load_tool_context",
                "tool_call_id": "call-load-1",
                "content": json.dumps(
                    {"ok": True, "tool_id": TOOL_ID, "tool_context_fingerprint": "tcf:fixture-presence"},
                    ensure_ascii=False,
                ),
            },
        ],
        "tool_names": ["exec", TOOL_ID, "load_tool_context"],
        "candidate_tool_names": [],
        "candidate_tool_items": [],
        "hydrated_tool_names": [TOOL_ID],
        "hydration_revoked_executor_names": [],
        "rbac_visible_tool_names": ["exec", TOOL_ID, "load_tool_context"],
        "visible_skill_ids": [],
        "candidate_skill_ids": [],
        "rbac_visible_skill_ids": [],
        "used_tools": [],
        "route_kind": "direct_reply",
        "parallel_enabled": False,
        "max_parallel_tool_calls": 1,
        "synthetic_tool_calls_used": False,
        "response_payload": {"content": "", "tool_calls": []},
        "frontdoor_request_body_messages": [],
        "frontdoor_history_shrink_reason": "",
        "frontdoor_stage_state": {
            "active_stage_id": "frontdoor-stage-1",
            "transition_required": False,
            "stages": [
                {
                    "stage_id": "frontdoor-stage-1",
                    "stage_index": 1,
                    "stage_kind": "normal",
                    "mode": "自主执行",
                    "status": "active",
                    "stage_goal": "inspect repository",
                    "completed_stage_summary": "",
                    "tool_round_budget": 4,
                    "tool_rounds_used": 1,
                    "key_refs": [],
                    "created_at": "2026-10-08T00:19:01+08:00",
                    "finished_at": "",
                    "rounds": [
                        {
                            "round_id": "frontdoor-stage-1:round-1",
                            "round_index": 1,
                            "tool_call_ids": ["call-load-1"],
                            "tools": [
                                {
                                    "tool_call_id": "call-load-1",
                                    "tool_name": "load_tool_context",
                                    "status": "success",
                                    "arguments": {"tool_id": TOOL_ID},
                                }
                            ],
                        }
                    ],
                }
            ],
        },
        "tool_call_payloads": [{"id": "call-stage-1", "name": STAGE_TOOL_NAME, "arguments": stage_arguments}],
    }


async def _run_graph_eviction_batch(monkeypatch, state: dict, *, tool_payload_getter=None) -> dict:
    main_task_service = None if tool_payload_getter is None else SimpleNamespace(get_tool_toolskill=tool_payload_getter)
    runner = create_agent_impl.CreateAgentCeoFrontDoorRunner(loop=SimpleNamespace(main_task_service=main_task_service))
    monkeypatch.setattr(runner, "_registered_tools_for_state", lambda state: {})
    monkeypatch.setattr(runner, "_build_tool_runtime_context", lambda **kwargs: {"on_progress": None})

    async def _fake_execute_tool_call_with_raw_result(*, tool, tool_name, arguments, runtime_context, on_progress, tool_call_id):
        _ = tool_name, runtime_context, on_progress, tool_call_id
        raw_result = await tool.execute(**arguments)
        return (
            raw_result,
            json.dumps(raw_result, ensure_ascii=False),
            "success",
            "2026-10-08T00:19:22+08:00",
            "2026-10-08T00:19:23+08:00",
            1.0,
        )

    monkeypatch.setattr(runner, "_execute_tool_call_with_raw_result", _fake_execute_tool_call_with_raw_result)
    return await runner._graph_execute_tools(state, runtime=SimpleNamespace(context=SimpleNamespace()))


def _surviving_tool_call_ids(result: dict) -> set[str]:
    return {
        str(item.get("tool_call_id") or "")
        for item in list(result.get("messages") or [])
        if str(item.get("role") or "") == "tool"
    }


async def test_graph_execute_tools_records_revocation_in_the_eviction_batch(monkeypatch) -> None:
    """裁撤落地的那一批就必须把撤销写回台账，不能等下一批或下一回合。

    实盘 web:ceo-1e834a45b8e7（main 45b10c13）：`submit_next_stage(drop=true, 带总结)`
    这一跳把阶段裁了，随后尾部契约与派发名单都少了它，但 `hydration_revoked_executor_names`
    全空、`hydrated_tool_names` 仍留着它 ⇒ 模型再 load 拿到 `already_callable` 而没有正文，
    执行侧回 `tool not available`——正是文档禁止的那对组合，第四态没消除。
    根因是节点体内的先后：工具态写回读的是裁撤前的阶段视图，而阶段账本在它之后才算完。
    """
    result = await _run_graph_eviction_batch(monkeypatch, _eviction_batch_state())

    # 夹具自检：裁撤确实落在这一批里（标记落了、正文行离开了发送基线）
    evicted = result["frontdoor_stage_state"]["stages"][0]
    assert evicted["context_evicted"] is True
    assert "call-load-1" not in _surviving_tool_call_ids(result)

    assert result["hydration_revoked_executor_names"] == [TOOL_ID]
    assert TOOL_ID not in list(result["hydrated_tool_names"] or [])
    assert TOOL_ID not in list(result["tool_names"] or [])
    assert TOOL_ID in list(result["candidate_tool_names"] or [])
    # 提升门禁与重复读守卫读的是这份候选视图：含它 ⇒ 下一跳 load 答 `candidate_hit`
    # 并当场提升，不会再给出没有正文的 `already_callable` 回执。
    assert TOOL_ID in CeoFrontDoorRuntimeOps._frontdoor_candidate_tool_view(result)


async def test_graph_execute_tools_keeps_named_contract_in_the_eviction_batch(monkeypatch) -> None:
    """同批点名 `keep_tools` ⇒ 正文行虽被裁走，台账不撤销、块里带着正文。

    写回到裁后视图的反面用例：在场证据从 loader 行换成 `kept_tool_contexts`，判据读不到
    那份账本就会把模型点名留住的工具一起撤掉——留不留得住只看这一条。
    """

    def _payload_getter(tool_id: str) -> dict:
        return {
            "tool_id": tool_id,
            "content": f"# {tool_id}\n\n保留的契约正文\n",
            "parameter_contract_markdown": "",
            "required_parameters": [],
            "example_arguments": {},
            "warnings": [],
            "errors": [],
        }

    result = await _run_graph_eviction_batch(
        monkeypatch,
        _eviction_batch_state(keep_tools=[TOOL_ID]),
        tool_payload_getter=_payload_getter,
    )

    evicted = result["frontdoor_stage_state"]["stages"][0]
    assert evicted["context_evicted"] is True
    assert [item["tool_id"] for item in list(evicted.get("kept_tool_contexts") or [])] == [TOOL_ID]
    assert "call-load-1" not in _surviving_tool_call_ids(result)
    assert result["hydration_revoked_executor_names"] == []
    assert TOOL_ID in list(result["hydrated_tool_names"] or [])
    assert TOOL_ID in list(result["tool_names"] or [])
    assert TOOL_ID not in list(result["candidate_tool_names"] or [])
    # 块里必须真的带着正文：只有标题没有正文的保留契约等于把撤销藏进渲染里。
    rendered = "\n".join(str(item.get("content") or "") for item in list(result["messages"] or []))
    assert KEPT_CONTRACT_HEADING in rendered
    assert "保留的契约正文" in rendered
