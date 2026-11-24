"""前门/记忆车道的轻量回合控制器。

复用 `NodeTurnController` 已硬化的组内换人/释放/观测方法（它们只委托 balancer + 操作
lease），仅把**首次授予**从节点准入队列泵改成同步 direct select：不排队、不起泵、不占
节点回合闸，保护交互聊天延迟。选人与 permit 仍本地，跨进程共享的只是上游观测（见
`model_load_balancer` 的共享视图）。

拿不到候选时返回 None，调用方退回扁平候选链发送（降级而不阻塞）。
"""

from __future__ import annotations

from datetime import datetime

from loguru import logger

from main.runtime.model_route import (
    ModelRoutePlan,
    RouteCandidateFilters,
)
from main.runtime.node_turn_controller import NodeTurnController, NodeTurnLease


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class FrontdoorTurnController(NodeTurnController):
    """无队列泵的回合控制器：首次授予同步 direct select，其余行为继承父类。"""

    def poke(self) -> None:
        # 没有队列泵可唤醒。
        return

    async def acquire_turn(
        self,
        *,
        task_id: str,
        node_id: str,
        model_ref: str = "",
        route_plan: ModelRoutePlan | None = None,
        filters: RouteCandidateFilters | None = None,
    ) -> NodeTurnLease | None:
        normalized_task_id = str(task_id or "").strip()
        normalized_node_id = str(node_id or "").strip()
        normalized_model_ref = str(model_ref or "").strip()
        if not normalized_node_id:
            raise ValueError("node_id is required for frontdoor turn selection")
        if route_plan is None and not normalized_model_ref:
            raise ValueError("model_ref or route_plan is required")
        effective_filters = filters or RouteCandidateFilters()
        with self._lock:
            return self._select_locked(
                task_id=normalized_task_id,
                node_id=normalized_node_id,
                model_ref=normalized_model_ref,
                route_plan=route_plan,
                filters=effective_filters,
            )

    def _select_locked(
        self,
        *,
        task_id: str,
        node_id: str,
        model_ref: str,
        route_plan: ModelRoutePlan | None,
        filters: RouteCandidateFilters,
    ) -> NodeTurnLease | None:
        """与节点道 `_try_grant` 同口径的首次选择，但同步、无队列。粘滞优先（rebind=False）。"""
        if route_plan is None:
            permit = self._model_concurrency_controller.try_acquire_first_available(model_ref=model_ref)
            if permit is None:
                return None
            return self._build_lease(
                task_id=task_id,
                node_id=node_id,
                model_ref=str(permit.model_ref or model_ref),
                key_index=int(permit.key_index),
                initial_model_permit=permit,
            )

        for route in route_plan.routes:
            if route.is_load_balance and self._balancer is not None:
                route_lease, _busy_reason = self._balancer.select(
                    node_id=node_id,
                    route_index=int(route.index),
                    group_key=str(route.group_key),
                    filters=filters,
                    task_id=task_id,
                )
                if route_lease is None:
                    continue
                return self._build_lease(
                    task_id=task_id,
                    node_id=node_id,
                    model_ref=str(route_lease.model_key),
                    key_index=int(route_lease.key_index),
                    initial_model_permit=route_lease.permit,
                    route_plan=route_plan,
                    route_index=int(route.index),
                    group_key=str(route_lease.group_key),
                    route_lease=route_lease,
                    filters=filters,
                )
            permit = self._model_concurrency_controller.try_acquire_first_available(model_ref=str(route.model_key or ""))
            if permit is None:
                continue
            return self._build_lease(
                task_id=task_id,
                node_id=node_id,
                model_ref=str(permit.model_ref or route.model_key),
                key_index=int(permit.key_index),
                initial_model_permit=permit,
                route_plan=route_plan,
                route_index=int(route.index),
            )
        return None

    def _build_lease(
        self,
        *,
        task_id: str,
        node_id: str,
        model_ref: str,
        key_index: int,
        initial_model_permit,
        route_plan: ModelRoutePlan | None = None,
        route_index: int = 0,
        group_key: str = "",
        route_lease=None,
        filters: RouteCandidateFilters | None = None,
    ) -> NodeTurnLease:
        self._next_lease_id += 1
        lease = NodeTurnLease(
            lease_id=self._next_lease_id,
            task_id=task_id,
            node_id=node_id,
            model_ref=str(model_ref or ""),
            key_index=int(key_index),
            acquired_at=_now_iso(),
            initial_model_permit=initial_model_permit,
            route_index=int(route_index),
            group_key=str(group_key or ""),
            route_plan=route_plan,
            route_lease=route_lease,
            route_filters=filters,
            admission_route_index=int(route_lease.route_index) if route_lease is not None else -1,
            admission_group_key=str(route_lease.group_key) if route_lease is not None else "",
        )
        self._running_leases[lease.lease_id] = lease
        if route_lease is not None:
            logger.info(
                "Model route selected (frontdoor): group={} selected_model_key={} route_index={} "
                "selection_reason={} rolling_rpm_60s={} penalty_429={} load_score={} node_id={}",
                route_lease.group_key,
                route_lease.model_key,
                int(route_lease.route_index),
                route_lease.selection_reason,
                int(route_lease.rolling_rpm),
                round(float(route_lease.penalty_before), 4),
                float(route_lease.score),
                lease.node_id,
            )
        return lease
