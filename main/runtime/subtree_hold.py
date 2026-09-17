"""子树分发屏障（subtree barrier）的 hold 判定。

定向通知把分发冻结范围从整个任务缩小为「以目标节点为根的子树」。
hold 判定是唯一的冻结真源，被三处检查点共用：

- ``NodeRunner.run_node`` 入口与回环后检查；
- ``react_loop._check_pause_or_cancel`` 的每个安全边界；
- 驱动器/门控读取 ``runtime_meta.distribution`` 的各处。

设计要点：

1. 状态集合：分发进行中（5 个活跃态）+ ``failed``（失败后子树保持冻结，
   直到操作员显式恢复把状态降级为 ``resume_ready``）。
2. ``blocked_node_ids`` 是入队快照 + 每波重推的并集；为了兜住 drain 期间
   新物化、还没进快照的子孙节点，判定还会沿 ``parent_node_id`` 链实时
   上溯：节点自身是目标、或任一祖先是目标 ⇒ 处于子树内 ⇒ hold。
3. ``distributing`` 状态下 frontier 成员豁免（它们走分发控制回合而不是
   被冻结），与 ``run_node`` 中「分发分支先于 hold 检查」的顺序互为保险。
4. 陈旧 meta 防御（A1）：runtime meta 是缓存、epochs 表是权威。调用方可注入
   ``get_epoch_state``，命中 hold 后回查 epoch 的库内状态——已终态
   （completed/cancelled/cancelled_by_task_delete）或查无（'none'）说明 meta
   脱同步，不冻结并经 ``on_stale_hold`` 告警；``failed`` 不在陈旧之列
   （按设计持续冻结等操作员恢复，见要点 1）。校验回调自身异常时保守维持
   hold（fail-safe）。
5. 未物化 spawn 轮豁免：屏障 drain 会等「在飞 spawn 轮的子节点物化」
   （``_barrier_materialize_pending_entries``），而物化只能由该父节点自己的
   协程产出（``_materialize_spawn_batch_children`` / ``_run_child_pipeline``）。
   若 hold 在这个协程跑到物化之前就把它中止，drain 就会等一个只有「释放屏障」
   才能产生的结果——互等死锁（2026-09-18 task:d596a609bbb3 事故）。因此
   ``spawn_round_has_unmaterialized_entries`` 命中的节点：检查点延后中止、
   恢复优先级不拦其同 id 重放、driver 在 drain 期间把它踢起来收尾。
   判据只放宽「何时停」，不放宽「停在哪」——物化一完成，豁免立即失效。
"""

from __future__ import annotations

from typing import Any, Callable

DISTRIBUTION_ACTIVE_STATES = frozenset({
    'pause_requested',
    'barrier_requested',
    'paused',
    'barrier_draining',
    'distributing',
})
DISTRIBUTION_HOLD_STATES = DISTRIBUTION_ACTIVE_STATES | {'failed'}
# A1 陈旧判定：epoch 库内状态落在该集合（或解析为 'none'/空）时，meta 的 hold 视为脱同步。
# 注意 'failed' 故意不在其中——失败 epoch 的子树按设计保持冻结（见模块要点 1/4）。
STALE_HOLD_EPOCH_STATES = frozenset({
    'completed',
    'cancelled',
    'canceled',
    'cancelled_by_task_delete',
    'none',
})
# 决策回合 resume_execution 的结果标记（node_runner 产出、task_actor_service 消费）。
INSPECTION_RESUME_MARKER = 'inspection_resume_execution'
# 合成验收中断结果的 blocking_reason：验收回绝处理据此走「不消耗拒绝预算、
# 不发验收→执行交接消息」分支（通知本身就是给执行节点的消息）。
NOTICE_INTERRUPT_REASON = 'acceptance interrupted by user notice (distribution decision: resume execution)'


def _id_set(values: Any) -> set[str]:
    return {
        str(item or '').strip()
        for item in list(values or [])
        if str(item or '').strip()
    }


def make_epoch_state_lookup(store: Any, task_id: str) -> Callable[[str], str]:
    """A1 接线用：epoch_id → epochs 表库内状态。

    - 具体 epoch id：查无记录返回 'none'（视为陈旧）。
    - 空/'active'（meta 未记 id 的兜底形态）：扫描该任务全部 epoch，取最早的
      非终态（queued/活跃/failed）者状态；一个都没有则 'none'。
    仅在 hold 命中时被调用（罕见路径），常规检查点零额外开销。
    """
    normalized_task_id = str(task_id or '').strip()

    def _lookup(epoch_id: str) -> str:
        normalized_epoch_id = str(epoch_id or '').strip()
        if normalized_epoch_id and normalized_epoch_id != 'active':
            epoch = store.get_task_message_distribution_epoch(normalized_task_id, normalized_epoch_id)
            if epoch is None:
                return 'none'
            return str(getattr(epoch, 'state', '') or '').strip().lower() or 'none'
        non_terminal = DISTRIBUTION_HOLD_STATES | {'queued'}
        for epoch in list(store.list_active_task_message_distribution_epochs(normalized_task_id) or []):
            state = str(getattr(epoch, 'state', '') or '').strip().lower()
            if state in non_terminal:
                return state or 'active'
        return 'none'

    return _lookup


def make_stale_hold_logger(warn: Callable[[str], None]) -> Callable[[str, str, str], None]:
    """A1 接线用：陈旧 hold 告警回调（吞掉日志失败，不影响判定）。"""

    def _on_stale_hold(node_id: str, epoch_id: str, db_state: str) -> None:
        try:
            warn(
                'stale subtree hold ignored (runtime meta/epochs desync): '
                f'node={node_id} epoch={epoch_id} db_state={db_state or "none"}'
            )
        except Exception:
            pass

    return _on_stale_hold


def _validated_hold_epoch_id(
    candidate_epoch_id: str,
    *,
    node_id: str,
    get_epoch_state: Callable[[str], str] | None,
    on_stale_hold: Callable[[str, str, str], None] | None,
) -> str:
    """A1：命中 hold 后按 epochs 表校验 meta 是否陈旧；陈旧则不冻结并告警。"""
    if not candidate_epoch_id or get_epoch_state is None:
        return candidate_epoch_id
    try:
        db_state = str(get_epoch_state(candidate_epoch_id) or '').strip().lower() or 'none'
    except Exception:
        # 校验通道自身故障时保守维持 hold（fail-safe），绝不误放行。
        return candidate_epoch_id
    if db_state not in STALE_HOLD_EPOCH_STATES:
        # 活跃态/failed（设计性冻结）/未知状态一律保守维持 hold。
        return candidate_epoch_id
    try:
        if on_stale_hold is not None:
            on_stale_hold(node_id, candidate_epoch_id, db_state)
    except Exception:
        pass
    return ''


def resolve_subtree_hold_epoch_id(
    *,
    distribution: dict[str, Any] | None,
    get_node: Callable[[str], Any],
    node_id: str,
    get_epoch_state: Callable[[str], str] | None = None,
    on_stale_hold: Callable[[str, str, str], None] | None = None,
) -> str:
    """返回应冻结该节点的 epoch id；节点不在任何活动子树屏障内时返回空串。

    ``get_epoch_state``/``on_stale_hold`` 为 A1 陈旧 meta 防御的可选注入点：
    仅在命中 hold 时回查一次 epoch 库内状态（罕见路径，不增加常规开销）。
    """
    dist = dict(distribution or {}) if isinstance(distribution, dict) else {}
    state = str(dist.get('state') or '').strip()
    if state not in DISTRIBUTION_HOLD_STATES:
        return ''
    normalized_node_id = str(node_id or '').strip()
    if not normalized_node_id:
        return ''
    epoch_id = str(dist.get('active_epoch_id') or '').strip() or 'active'
    if state == 'distributing' and normalized_node_id in _id_set(dist.get('frontier_node_ids')):
        return ''
    if normalized_node_id in _id_set(dist.get('blocked_node_ids')):
        return _validated_hold_epoch_id(
            epoch_id,
            node_id=normalized_node_id,
            get_epoch_state=get_epoch_state,
            on_stale_hold=on_stale_hold,
        )
    targets = _id_set(dist.get('target_node_ids'))
    if not targets:
        return ''
    seen: set[str] = set()
    current_id = normalized_node_id
    while current_id and current_id not in seen:
        if current_id in targets:
            return _validated_hold_epoch_id(
                epoch_id,
                node_id=normalized_node_id,
                get_epoch_state=get_epoch_state,
                on_stale_hold=on_stale_hold,
            )
        seen.add(current_id)
        node = get_node(current_id)
        current_id = str(getattr(node, 'parent_node_id', '') or '').strip() if node is not None else ''
    return ''


def spawn_entry_child_fully_materialized(
    *,
    task_id: str,
    parent_node_id: str,
    round_id: str,
    entry_index: int,
    entry: dict[str, Any],
    child: Any,
    is_in_live_tree: Callable[[str], bool] | None = None,
) -> bool:
    """spawn entry 是否已完整物化到「由本父节点本轮的该 entry 拥有」的子节点。

    字段级判定（child 归属、``spawn_owner_kind='child'``、parent/round/entry_index
    三项匹配）是单一真源，驱动器与检查点共用，避免两处判定漂移。
    ``is_in_live_tree`` 为可选的活动分发树回调：驱动器传它做更严的判据；拿不到时
    省略——省略只会让判定更宽松（更早认定「已物化」），即更早允许冻结，
    与既有行为一致，不会引入「该冻却不冻」。
    """
    child_node_id = str(entry.get('child_node_id') or '').strip()
    if not child_node_id or child is None:
        return False
    if str(getattr(child, 'task_id', '') or '').strip() != str(task_id or '').strip():
        return False
    metadata = dict(getattr(child, 'metadata', None) or {}) if isinstance(getattr(child, 'metadata', None), dict) else {}
    if str(metadata.get('spawn_owner_kind') or '').strip().lower() != 'child':
        return False
    if str(metadata.get('spawn_owner_parent_node_id') or '').strip() != str(parent_node_id or '').strip():
        return False
    if str(metadata.get('spawn_owner_round_id') or '').strip() != str(round_id or '').strip():
        return False
    try:
        owner_entry_index = int(metadata.get('spawn_owner_entry_index'))
    except (TypeError, ValueError):
        return False
    if owner_entry_index != int(entry_index):
        return False
    if is_in_live_tree is not None:
        try:
            return bool(is_in_live_tree(child_node_id))
        except Exception:
            return False
    return True


def spawn_round_has_unmaterialized_entries(
    *,
    get_node: Callable[[str], Any],
    task_id: str,
    node: Any,
    is_in_live_tree: Callable[[str], bool] | None = None,
) -> bool:
    """节点是否持有「未完成且仍有未物化 entry」的 spawn 轮。

    判据与驱动器 drain 的 ``_barrier_materialize_pending_entries`` 同源：未完成轮里
    存在 ``status ∈ {queued, running}``、未被 spawn review 拦下（``review_decision``
    非 ``blocked``）、且未完整物化的 entry。被全部拦下的轮永远不会物化，因此不算
    「待物化」——否则节点再也不会被冻结（要点 5）。
    """
    parent_node_id = str(getattr(node, 'node_id', '') or '').strip()
    if not parent_node_id:
        return False
    metadata = dict(getattr(node, 'metadata', None) or {}) if isinstance(getattr(node, 'metadata', None), dict) else {}
    operations = metadata.get('spawn_operations')
    if not isinstance(operations, dict):
        return False
    for raw_round_id, raw_payload in operations.items():
        if not isinstance(raw_payload, dict) or bool(raw_payload.get('completed')):
            continue
        round_id = str(raw_round_id or '').strip()
        for fallback_index, raw_entry in enumerate(list(raw_payload.get('entries') or [])):
            if not isinstance(raw_entry, dict):
                continue
            if str(raw_entry.get('review_decision') or '').strip().lower() == 'blocked':
                continue
            if str(raw_entry.get('status') or '').strip().lower() not in {'queued', 'running'}:
                continue
            child_node_id = str(raw_entry.get('child_node_id') or '').strip()
            child = get_node(child_node_id) if child_node_id else None
            try:
                entry_index = int(raw_entry.get('index') or fallback_index)
            except (TypeError, ValueError):
                entry_index = fallback_index
            if spawn_entry_child_fully_materialized(
                task_id=task_id,
                parent_node_id=parent_node_id,
                round_id=round_id,
                entry_index=entry_index,
                entry=raw_entry,
                child=child,
                is_in_live_tree=is_in_live_tree,
            ):
                continue
            return True
    return False
