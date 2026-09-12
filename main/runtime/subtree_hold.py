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


def resolve_subtree_hold_epoch_id(
    *,
    distribution: dict[str, Any] | None,
    get_node: Callable[[str], Any],
    node_id: str,
) -> str:
    """返回应冻结该节点的 epoch id；节点不在任何活动子树屏障内时返回空串。"""
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
        return epoch_id
    targets = _id_set(dist.get('target_node_ids'))
    if not targets:
        return ''
    seen: set[str] = set()
    current_id = normalized_node_id
    while current_id and current_id not in seen:
        if current_id in targets:
            return epoch_id
        seen.add(current_id)
        node = get_node(current_id)
        current_id = str(getattr(node, 'parent_node_id', '') or '').strip() if node is not None else ''
    return ''
