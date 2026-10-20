"""逐跳路由归属落库的契约测：字段必须活着走完写入链，而不是只在生产端存在。

这些用例故意**分两层**：
- 生产端（balancer / 租约 / 归一化）证明值真的算出来并挂上了；
- 载体端（attempt 归一化 → usage 合并）证明路上每一层"按显式字段清单重建对象"的地方
  都没有把它悄悄丢成默认值。丢字段的症状是不报错、那一格永远是哨兵，所以只能这样钉。
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from g3ku.providers.base import LLMModelAttempt
from main.runtime.chat_backend import _normalize_model_attempts
from main.runtime.model_load_balancer import (
    ModelLoadBalancer,
    classify_throttle_dimension,
)

# 基线红证要求这些用例在**未修改的树**上也能被收集（collect error 不算红测）。
# 只有本次新增的符号走本地 shim：旧树上 shim 成立、断言各自失败，新树上走真实现。
try:  # pragma: no cover - 存在性由实现决定
    from main.runtime.model_load_balancer import THROTTLE_DIMENSION_TPM_RPM
except ImportError:  # pragma: no cover
    THROTTLE_DIMENSION_TPM_RPM = "tpm_rpm"
try:  # pragma: no cover
    from main.runtime.model_load_balancer import find_group_drift
except ImportError:  # pragma: no cover
    find_group_drift = lambda groups: []  # noqa: E731
from main.runtime.model_route import (
    ModelRouteLease,
    ResolvedLoadBalanceGroup,
    RouteMemberView,
    _member_view_for,
)
from main.runtime.node_turn_controller import NodeTurnController, NodeTurnLease
from main.token_usage import build_token_usage_from_attempts, merge_token_usage_by_model


class _FakePermits:
    def __init__(self) -> None:
        self.running: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire_least_loaded(self, *, model_ref: str) -> object:
        with self._lock:
            current = int(self.running.get(model_ref, 0))
            self.running[model_ref] = current + 1
            return type("_Permit", (), {"model_ref": model_ref, "key_index": 0})()

    def release(self, permit: object) -> None:
        model_ref = str(getattr(permit, "model_ref", "") or "")
        with self._lock:
            self.running[model_ref] = max(0, int(self.running.get(model_ref, 0)) - 1)

    def model_state(self, model_ref: str) -> dict[str, object]:
        return {"running": {0: int(self.running.get(model_ref, 0))}, "waiting": {0: 0}, "key_count": 1}

    def effective_capacity(self, model_ref: str) -> int | None:
        return None


def _members(*keys: str, pool_key: str = "") -> list[RouteMemberView]:
    return [
        RouteMemberView(
            model_key=key,
            enabled=True,
            context_window_tokens=200000,
            image_multimodal_enabled=True,
            quota_pool_key=pool_key,
        )
        for key in keys
    ]


def _group(group_key: str, *keys: str) -> ResolvedLoadBalanceGroup:
    return ResolvedLoadBalanceGroup(
        group_key=group_key,
        enabled=True,
        max_retry_rounds=6,
        members=_members(*keys),
    )


def _balancer(*groups: ResolvedLoadBalanceGroup, buckets: dict[str, list[str]] | None = None) -> ModelLoadBalancer:
    balancer = ModelLoadBalancer(
        permit_source=_FakePermits(),
        monotonic=lambda: 1000.0,
        resolve_quota_buckets=(lambda model_key: list(buckets.get(model_key, [f"key:{model_key}"])) if buckets else None),
    )
    balancer.configure(groups={group.group_key: group for group in groups}, config_revision=3)
    return balancer


def test_route_identity_survives_attempt_normalization() -> None:
    """`_normalize_model_attempts` 逐字段重建 attempt：新字段不报进去就静默变哨兵。"""
    attempts = [
        LLMModelAttempt(
            model_key="deepseek-v4-flash-3",
            provider_id="p",
            provider_model="m",
            route_index=1,
            group_key="v4",
            quota_bucket_index=0,
        ),
        LLMModelAttempt(model_key="direct-model", provider_id="p", provider_model="m"),
    ]

    normalized = _normalize_model_attempts(attempts)

    assert [(a.route_index, a.group_key, a.quota_bucket_index) for a in normalized] == [
        (1, "v4", 0),
        (-1, "", -1),
    ]


def test_route_identity_survives_usage_merge_and_takes_first_declared() -> None:
    """合并按 model_key 分组：归属取首个非哨兵，不能被合并成 0/''。"""
    attempts = [
        LLMModelAttempt(model_key="m", provider_id="p", provider_model="q", route_index=1, group_key="v4", quota_bucket_index=2),
        LLMModelAttempt(model_key="m", provider_id="p", provider_model="q"),
    ]

    _summary, per_model = build_token_usage_from_attempts(_normalize_model_attempts(attempts), tracked=True)
    merged = merge_token_usage_by_model(per_model, tracked=True)

    assert len(merged) == 1
    record = merged[0]
    assert (record.route_index, record.group_key, record.quota_bucket_index) == (1, "v4", 2)
    assert record.call_count == 2


def test_route_record_defaults_are_sentinels_not_zero() -> None:
    """哨兵必须是 -1/''，不能是 0：0 是合法的链首格号，写成 0 就是把"无租约"读成"第一档"。"""
    from main.models import ModelTokenUsageRecord

    record = ModelTokenUsageRecord()

    assert record.route_index == -1
    assert record.quota_bucket_index == -1
    assert record.group_key == ""


def test_quota_bucket_index_is_shared_across_one_bucket_and_split_across_two() -> None:
    balancer = _balancer(
        _group("flash", "m_a", "m_b", "m_c"),
        buckets={"m_a": ["key:1"], "m_b": ["key:1"], "m_c": ["key:2"]},
    )

    leases = {}
    for node_index, model_key in enumerate(("m_a", "m_b", "m_c")):
        lease, reason = balancer.select(
            node_id=f"node:{node_index}",
            route_index=0,
            group_key="flash",
            task_id="task:t",
        )
        assert lease is not None, reason
        leases[model_key] = lease

    # 同一桶的两颗序号相同，另一桶不同；三个成员轮流选过，才能确认不是恒 0。
    assert leases["m_a"].quota_bucket_index == leases["m_b"].quota_bucket_index
    assert leases["m_c"].quota_bucket_index not in (leases["m_a"].quota_bucket_index,)
    assert all(lease.quota_bucket_index >= 0 for lease in leases.values())


def test_throttle_dimension_keeps_compound_text_in_its_own_bucket() -> None:
    assert classify_throttle_dimension("inference exceeds tpm/rpm limit") == THROTTLE_DIMENSION_TPM_RPM
    assert classify_throttle_dimension("rpm exhausted") == "rpm"
    assert classify_throttle_dimension("tpm exhausted") == "tpm"
    assert classify_throttle_dimension("rps exhausted") == "rps"


def test_group_drift_findings_flag_key_mismatch_and_shared_member() -> None:
    clean = {"flash": _group("flash", "m_a"), "v4": _group("v4", "m_b")}
    assert find_group_drift(clean) == []
    assert find_group_drift({}) == []

    wrong_key = {"v4": _group("seva", "m_b")}
    assert any("key=v4 declares group=seva" in item for item in find_group_drift(wrong_key))

    shared_member = {"flash": _group("flash", "m_a"), "v4": _group("v4", "m_a")}
    findings = find_group_drift(shared_member)
    assert any("member=m_a owned by flash and v4" in item for item in findings)


def test_member_view_carries_quota_pool_key() -> None:
    """字段声明了却从不填 ⇒ 下游基于它做桶感知时静默读到空串。"""
    config = SimpleNamespace(
        get_managed_model=lambda key: SimpleNamespace(
            enabled=True,
            context_window_tokens=1234,
            image_multimodal_enabled=False,
            quota_pool_key=" shared-account ",
        )
    )

    view = _member_view_for(config, "m_a")

    assert view.quota_pool_key == "shared-account"


def test_turn_lease_keeps_admission_and_records_escalation_served_and_dimensions() -> None:
    balancer = _balancer(_group("flash", "m_a"), _group("v4", "m_b"))
    controller = NodeTurnController(model_concurrency_controller=object(), balancer=balancer)
    admission_lease, reason = balancer.select(node_id="node:1", route_index=0, group_key="flash", task_id="task:1")
    assert admission_lease is not None, reason

    lease = NodeTurnLease(
        lease_id=1,
        task_id="task:1",
        node_id="node:1",
        model_ref="m_a",
        key_index=0,
        acquired_at="2026-10-20T00:00:00+08:00",
        route_index=0,
        group_key="flash",
        route_plan=SimpleNamespace(
            route_at=lambda index: SimpleNamespace(is_load_balance=True, group_key=("flash" if index == 0 else "v4"))
        ),
        route_lease=admission_lease,
        admission_route_index=0,
        admission_group_key="flash",
    )

    controller.record_route_request_start(lease)
    assert (lease.served_route_index, lease.served_group_key) == (0, "flash")

    controller.record_route_outcome(lease, status_code=429, error_text="Error code: 429 - tpm exhausted")
    controller.record_route_outcome(lease, status_code=429, error_text="Error code: 429 - rps exhausted")
    controller.record_route_outcome(lease, status_code=500, error_text="boom")
    assert lease.throttle_dimensions == ["tpm", "rps"]

    rebound = controller.rebind_turn(lease, route_index=1, rebind_reason="route_entry")
    assert rebound is not None
    # 升档改写当前绑定的档，但准入档保持原值——这正是台账要能算出"跳了几档"的前提。
    assert (lease.route_index, lease.group_key) == (1, "v4")
    assert (lease.admission_route_index, lease.admission_group_key) == (0, "flash")
    assert (lease.escalation_count, lease.escalation_reason) == (1, "route_entry")

    controller.record_route_request_start(lease)
    assert (lease.served_route_index, lease.served_group_key) == (1, "v4")

    # 组内换人（同一格号）不算跨档，不能把计数加上去。
    before = lease.escalation_count
    assert controller.rebind_turn(lease, route_index=1, rebind_reason="fallback_after_failure") is not None
    assert lease.escalation_count == before


def test_group_drift_warning_fires_once_per_change_not_per_refresh(monkeypatch) -> None:
    """配置会周期性重刷：漂移集合没变就不该再印第二行，否则告警被刷成噪声。"""
    import main.runtime.model_load_balancer as balancer_module

    lines: list[str] = []
    monkeypatch.setattr(
        balancer_module.logger,
        "warning",
        lambda message, *args: lines.append(str(message).format(*args)),
    )
    drifting = {"v4": _group("seva", "m_b")}
    balancer = ModelLoadBalancer(permit_source=_FakePermits(), monotonic=lambda: 1000.0)

    balancer.configure(groups=drifting, config_revision=1)
    balancer.configure(groups=drifting, config_revision=2)
    assert len(lines) == 1

    balancer.configure(groups={"flash": _group("flash", "m_a")}, config_revision=3)
    assert len(lines) == 1


def test_route_lease_field_defaults_to_sentinel_for_non_balancer_lanes() -> None:
    lease = ModelRouteLease(
        lease_id=1,
        node_id="node:1",
        task_id="task:1",
        route_index=0,
        group_key="",
        model_key="m",
        key_index=0,
        quota_bucket_key="",
        selection_reason="least_load",
        config_revision=1,
    )

    assert lease.quota_bucket_index == -1


@pytest.mark.parametrize("group_key", ["flash", "v4"])
def test_group_drift_counter_exposed_on_snapshot(group_key: str) -> None:
    balancer = _balancer(_group("flash", "m_a"), _group("v4", "m_b"))
    snapshot = balancer.snapshot()

    assert snapshot["group_drift_count"] == 0
    assert isinstance(snapshot["group_drift_findings"], list)
