from __future__ import annotations

import threading

import pytest

from main.runtime.model_key_concurrency import ModelKeyConcurrencyController
from main.runtime.model_load_balancer import (
    PENALTY_HALF_LIFE_SECONDS,
    ModelLoadBalancer,
    classify_throttle_dimension,
    is_rate_limited,
    quota_bucket_key,
)
from main.runtime.model_route import (
    LEASE_OUTCOME_BUILD_FAILED,
    LEASE_OUTCOME_CANCELLED,
    LEASE_OUTCOME_SUCCESS,
    ModelRouteLease,
    ResolvedLoadBalanceGroup,
    RouteCandidateFilters,
    RouteMemberView,
)


class _FakePermits:
    """最小 permit 源：按 model 记在飞数，容量可按模型分别注入。

    `capacity` 的每个值是 `(每 key 上限, key 数)`；未列出的模型用构造时的默认值。
    """

    def __init__(self, *, per_key_limit: int | None = None, key_count: int = 1, capacity: dict[str, tuple[int | None, int]] | None = None) -> None:
        self.default_limit = per_key_limit
        self.default_keys = key_count
        self.capacity = dict(capacity or {})
        self.running: dict[str, int] = {}
        self.released: dict[str, int] = {}
        self.acquire_calls: list[str] = []
        self._lock = threading.Lock()

    def _limits(self, model_ref: str) -> tuple[int | None, int]:
        limit, keys = self.capacity.get(model_ref, (self.default_limit, self.default_keys))
        return limit, max(1, int(keys))

    def acquire_least_loaded(self, *, model_ref: str) -> object | None:
        with self._lock:
            self.acquire_calls.append(str(model_ref))
            limit, keys = self._limits(model_ref)
            current = int(self.running.get(model_ref, 0))
            if limit is not None and current >= limit * keys:
                return None
            self.running[model_ref] = current + 1
            return type("_Permit", (), {"model_ref": model_ref, "key_index": current % keys})()

    def release(self, permit: object) -> None:
        if permit is None:
            return
        model_ref = str(getattr(permit, "model_ref", "") or "")
        with self._lock:
            self.running[model_ref] = max(0, int(self.running.get(model_ref, 0)) - 1)
            self.released[model_ref] = int(self.released.get(model_ref, 0)) + 1

    def model_state(self, model_ref: str) -> dict[str, object]:
        _limit, keys = self._limits(model_ref)
        return {"running": {0: int(self.running.get(model_ref, 0))}, "waiting": {0: 0}, "key_count": keys}

    def effective_capacity(self, model_ref: str) -> int | None:
        limit, keys = self._limits(model_ref)
        if limit is None:
            return None
        return int(limit) * int(keys)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _members(*keys: str, context: int = 200000, multimodal: bool = True) -> list[RouteMemberView]:
    return [
        RouteMemberView(
            model_key=key,
            enabled=True,
            context_window_tokens=context,
            image_multimodal_enabled=multimodal,
        )
        for key in keys
    ]


def _group(group_key: str, *keys: str, max_rounds: int = 1, **kwargs) -> ResolvedLoadBalanceGroup:
    return ResolvedLoadBalanceGroup(
        group_key=group_key,
        enabled=True,
        max_retry_rounds=max_rounds,
        members=_members(*keys, **kwargs),
    )


def _balancer(
    *groups: ResolvedLoadBalanceGroup,
    permits: _FakePermits | None = None,
    clock: _Clock | None = None,
    buckets: dict[str, list[str]] | None = None,
    revision: int = 7,
) -> ModelLoadBalancer:
    clock = clock or _Clock()
    balancer = ModelLoadBalancer(
        permit_source=permits if permits is not None else _FakePermits(),
        monotonic=clock,
        resolve_quota_buckets=(lambda model_key: list(buckets.get(model_key, [f"key:{model_key}"])) if buckets else None),
    )
    balancer.configure(groups={group.group_key: group for group in groups}, config_revision=revision)
    return balancer


def _select(balancer: ModelLoadBalancer, node_id: str, group_key: str = "g1", **kwargs) -> ModelRouteLease:
    lease, reason = balancer.select(node_id=node_id, group_key=group_key, route_index=0, task_id="task:x", **kwargs)
    assert lease is not None, f"selection failed: {reason}"
    return lease


def test_scale_spread_stays_even_across_hundred_nodes() -> None:
    """100 个节点首次准入到 3 成员等容量组：分布必须摊平，不允许出现热点。"""
    balancer = _balancer(_group("g1", "m_a", "m_b", "m_c"))

    bound = [_select(balancer, f"node:{index}").model_key for index in range(100)]

    counts = {key: bound.count(key) for key in ("m_a", "m_b", "m_c")}
    assert max(counts.values()) - min(counts.values()) <= 1
    assert all(30 <= value <= 40 for value in counts.values())
    # 连续到达的节点不能落在同一个成员上（今天的链首形态）。
    assert len(set(bound[:3])) == 3


def test_rate_limited_member_is_skipped_by_next_node_without_config_change() -> None:
    clock = _Clock()
    permits = _FakePermits()
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=permits, clock=clock)

    first = _select(balancer, "node:1")
    balancer.record_request_start(first)
    balancer.record_outcome(first, status_code=429, error_text="Error code: 429 - rpm limit")
    balancer.release(first, outcome=LEASE_OUTCOME_SUCCESS)

    second = _select(balancer, "node:2")

    assert second.model_key == "m_b"
    assert second.selection_reason == "least_load"


def test_first_bindings_spread_across_equal_capacity_members() -> None:
    balancer = _balancer(_group("g1", "m_a", "m_b", "m_c"))

    # 均衡粒度是节点：每个节点首次准入各选一次。
    bound = [_select(balancer, f"node:{index}").model_key for index in range(12)]

    counts = {key: bound.count(key) for key in ("m_a", "m_b", "m_c")}
    assert counts == {"m_a": 4, "m_b": 4, "m_c": 4}
    assert bound[0] == "m_a" and bound[1] != "m_a"


def test_tie_break_does_not_forever_pick_config_first_item() -> None:
    balancer = _balancer(_group("g1", "m_a", "m_b"))

    # 没有任何负载差异时，第二轮仍应从另一个成员开始，而不是每次都回到 m_a。
    first_round = [_select(balancer, f"node:{index}").model_key for index in range(2)]
    balancer.forget_node("node:0")
    balancer.forget_node("node:1")
    second_round = [_select(balancer, f"node:{index}").model_key for index in range(10, 12)]

    assert sorted(first_round) == ["m_a", "m_b"]
    assert sorted(second_round) == ["m_a", "m_b"]


def test_selection_uses_normalized_load_not_raw_running() -> None:
    # m_small 容量 1 已占满；m_big 容量 4 已占 3。raw running 上 m_big 更高，
    # 但归一化后 m_big 更空（0.75 < 1.0）。
    permits = _FakePermits(capacity={"m_small": (1, 1), "m_big": (1, 4)})
    balancer = _balancer(_group("g1", "m_small", "m_big"), permits=permits)

    permits.acquire_least_loaded(model_ref="m_small")
    for _ in range(3):
        assert permits.acquire_least_loaded(model_ref="m_big") is not None

    lease = _select(balancer, "node:1")

    assert lease.model_key == "m_big"
    assert lease.local_capacity == 4
    assert lease.running_before == 3


def test_reserved_reservation_is_atomic_under_concurrent_selection() -> None:
    permits = _FakePermits(per_key_limit=2, key_count=1)
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=permits)

    leases: list[ModelRouteLease] = []
    errors: list[BaseException] = []

    def _worker(index: int) -> None:
        try:
            lease, _reason = balancer.select(node_id=f"node:{index}", group_key="g1", route_index=0)
            if lease is not None:
                leases.append(lease)
        except BaseException as exc:  # noqa: BLE001 - 收集线程内异常给主线程断言
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    # 组容量 = 2 成员 × 2 permit = 4；超出容量的两个必须选不到（no_capacity），
    # 不允许把同一成员选超容量。
    assert len(leases) == 4
    assert sum(permits.running.get(key, 0) for key in ("m_a", "m_b")) == 4
    assert {lease.model_key for lease in leases} == {"m_a", "m_b"}


@pytest.mark.parametrize(
    "outcome",
    [LEASE_OUTCOME_SUCCESS, LEASE_OUTCOME_BUILD_FAILED, LEASE_OUTCOME_CANCELLED],
)
def test_release_is_exactly_once_on_every_path(outcome: str) -> None:
    permits = _FakePermits()
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=permits)

    lease = _select(balancer, "node:1")
    assert permits.running[lease.model_key] == 1
    assert balancer.snapshot()["groups"]["g1"]["members"][0]["reserved"] + balancer.snapshot()["groups"]["g1"]["members"][1]["reserved"] == 1

    balancer.release(lease, outcome=outcome)
    balancer.release(lease, outcome=outcome)

    assert permits.running[lease.model_key] == 0
    assert permits.released.get(lease.model_key, 0) == 1
    snapshot = balancer.snapshot()["groups"]["g1"]
    assert sum(member["reserved"] for member in snapshot["members"]) == 0


def test_unstarted_permit_is_released_without_touching_request_counters() -> None:
    permits = _FakePermits()
    balancer = _balancer(_group("g1", "m_a"), permits=permits)

    lease = _select(balancer, "node:1")
    balancer.release(lease, outcome=LEASE_OUTCOME_BUILD_FAILED)

    # 没发出去的预占既不该记 RPM 样本，也不该留下 reserved。
    assert balancer.snapshot()["groups"]["g1"]["members"][0]["rolling_rpm_60s"] == 0
    assert balancer.snapshot()["groups"]["g1"]["members"][0]["reserved"] == 0


def test_group_reports_busy_instead_of_waiting() -> None:
    permits = _FakePermits(per_key_limit=1, key_count=1)
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=permits)
    permits.acquire_least_loaded(model_ref="m_a")
    permits.acquire_least_loaded(model_ref="m_b")

    lease, reason = balancer.select(node_id="node:1", group_key="g1", route_index=0)

    assert lease is None
    assert reason == "no_capacity"


def test_unknown_and_disabled_group_are_distinguished() -> None:
    disabled = ResolvedLoadBalanceGroup(group_key="g_off", enabled=False, max_retry_rounds=1, members=_members("m_a"))
    balancer = _balancer(_group("g1", "m_a"), disabled)

    assert balancer.select(node_id="node:1", group_key="g_missing", route_index=0) == (None, "unknown_group")
    assert balancer.select(node_id="node:1", group_key="g_off", route_index=0) == (None, "group_disabled")


def test_node_binding_is_sticky_across_rounds() -> None:
    permits = _FakePermits()
    balancer = _balancer(_group("g1", "m_a", "m_b", "m_c"), permits=permits)

    first = _select(balancer, "node:sticky")
    releases = 0
    for _ in range(5):
        balancer.release(first, outcome=LEASE_OUTCOME_SUCCESS)
        releases += 1
        first = _select(balancer, "node:sticky")
        assert first.model_key == "m_a"
    assert releases == 5
    # 其他节点仍按负载摊开，说明粘滞没有把整组钉死在第一个成员上。
    others = [_select(balancer, f"node:{index}").model_key for index in range(100, 103)]
    assert set(others) == {"m_a", "m_b", "m_c"} or len(set(others)) > 1


def test_rebind_triggers_on_capacity_and_rebind_flag() -> None:
    permits = _FakePermits(per_key_limit=1, key_count=1)
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=permits)

    first = _select(balancer, "node:1")
    assert first.model_key == "m_a"
    balancer.release(first, outcome=LEASE_OUTCOME_SUCCESS)

    # 触发一：绑定成员拿不出 permit（外部先把 m_a 的容量占满），才换人。
    pinned = permits.acquire_least_loaded(model_ref="m_a")
    second = _select(balancer, "node:1")
    assert second.model_key == "m_b"
    assert second.sticky_rebind_reason == "capacity"
    balancer.release(second, outcome=LEASE_OUTCOME_SUCCESS)
    permits.release(pinned)

    # 触发二：显式重绑——组内换成员走的就是这个入口。
    third = _select(balancer, "node:1", rebind=True, rebind_reason="fallback_after_failure")
    assert third.sticky_rebind_reason == "fallback_after_failure"
    balancer.release(third, outcome=LEASE_OUTCOME_SUCCESS)


def test_non_rate_limit_failure_leaves_no_memory() -> None:
    """失败记忆只有一档：上游限流。其余一律当场交给链 fallback，不跨请求留存。

    这条断言钉的是设计而非疏漏——后来者若把「401 就先冷一段」当 bug 补回来，这里会红。
    """
    permits = _FakePermits()
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=permits)

    first = _select(balancer, "node:1")
    assert first.model_key == "m_a"
    balancer.record_outcome(first, status_code=None, error_text="AuthenticationError: Error code: 401 - bad key")
    balancer.record_outcome(first, status_code=None, error_text="All configured API keys are disabled for model m_a")
    balancer.release(first, outcome=LEASE_OUTCOME_SUCCESS)

    again = _select(balancer, "node:1")
    assert again.model_key == "m_a"
    assert again.sticky_rebind_reason == ""
    members = {row["model_key"]: row for row in balancer.snapshot()["groups"]["g1"]["members"]}
    assert members["m_a"]["penalty_429"] == 0.0
    balancer.release(again, outcome=LEASE_OUTCOME_SUCCESS)


def test_rate_limit_words_come_from_the_model_chain_table() -> None:
    """限流判据与旧链共用一张 `429` 关键字表，本模块不得自带文本。"""
    assert is_rate_limited(None, "Error code: 429 - rpm limit")
    assert is_rate_limited(None, "Too many requests")
    assert is_rate_limited(None, "quota exceeded")
    assert is_rate_limited(429, "")
    # 状态码不是 429、文本也不在表里 ⇒ 不算限流。
    assert not is_rate_limited(500, "upstream blew up")
    assert not is_rate_limited(None, "AuthenticationError: Error code: 401 - bad key")


def test_penalty_decays_and_does_not_permanently_pin_a_member() -> None:
    clock = _Clock()
    permits = _FakePermits()
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=permits, clock=clock)

    lease = _select(balancer, "node:1")
    balancer.record_request_start(lease)
    balancer.record_outcome(lease, status_code=429, error_text="RateLimitError: Error code: 429 - rpm limit")
    balancer.release(lease, outcome=LEASE_OUTCOME_SUCCESS)

    with_penalty = balancer.snapshot()["groups"]["g1"]["members"][0]
    assert with_penalty["penalty_429"] > 0.5

    clock.advance(PENALTY_HALF_LIFE_SECONDS * 7)
    after_decay = balancer.snapshot()["groups"]["g1"]["members"][0]
    assert after_decay["penalty_429"] < 0.01
    # 观测窗口之外不再惩罚，但速率样本同样已过期。
    assert after_decay["rolling_rpm_60s"] == 0


def test_rate_limit_penalty_is_shared_across_members_in_one_quota_bucket() -> None:
    clock = _Clock()
    permits = _FakePermits()
    buckets = {"m_a": ["key:shared"], "m_b": ["key:shared"]}
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=permits, clock=clock, buckets=buckets)

    first = _select(balancer, "node:1")
    assert first.model_key == "m_a"
    balancer.record_request_start(first)
    balancer.record_outcome(first, status_code=429, error_text="Error code: 429 - rpm exhausted")
    balancer.release(first, outcome=LEASE_OUTCOME_SUCCESS)

    # 同桶的 m_b 必须继承这份惩罚，否则两个 binding 会被当成两份独立容量。
    second = _select(balancer, "node:2")
    assert second.model_key == "m_b"
    members = {row["model_key"]: row for row in balancer.snapshot()["groups"]["g1"]["members"]}
    assert members["m_b"]["penalty_429"] > 0
    assert members["m_a"]["quota_bucket_index"] == members["m_b"]["quota_bucket_index"]
    assert balancer.snapshot()["groups"]["g1"]["quota_bucket_count"] == 1
    assert balancer.snapshot()["groups"]["g1"]["shared_bucket_member_count"] == 1


def test_unresolved_quota_identity_never_merges_members() -> None:
    permits = _FakePermits()
    # 解析不出密钥材料时返回空列表：必须各自成 unresolved 桶，不能互并成一个假桶。
    balancer = _balancer(
        _group("g1", "m_a", "m_b"),
        permits=permits,
        buckets={"m_a": [], "m_b": []},
    )

    snapshot = balancer.snapshot()["groups"]["g1"]
    assert snapshot["unresolved_bucket_count"] == 2
    assert snapshot["quota_bucket_count"] == 0
    assert snapshot["shared_bucket_member_count"] == 0


def test_quota_bucket_key_is_stable_and_never_leaks_material() -> None:
    key_one = quota_bucket_key(endpoint="https://gw.example/v1", api_key="sk-aaaaaaaaaaaaaaaa")
    key_two = quota_bucket_key(endpoint="https://gw.example/v1", api_key="sk-aaaaaaaaaaaaaaaa")
    key_other = quota_bucket_key(endpoint="https://gw.example/v1", api_key="sk-bbbbbbbbbbbbbbbb")

    assert key_one == key_two
    assert key_one != key_other
    assert "sk-" not in key_one and "gw.example" not in key_one
    assert quota_bucket_key(endpoint="", api_key="") == ""
    assert quota_bucket_key(endpoint="https://gw.example/v1", api_key="sk-x", quota_pool_key="gw60") == "pool:gw60"


def test_context_and_multimodal_filters_exclude_members() -> None:
    balancer = _balancer(
        ResolvedLoadBalanceGroup(
            group_key="g1",
            enabled=True,
            max_retry_rounds=1,
            members=[
                RouteMemberView(model_key="m_small", context_window_tokens=32000, image_multimodal_enabled=False),
                RouteMemberView(model_key="m_big", context_window_tokens=200000, image_multimodal_enabled=True),
            ],
        )
    )

    lease = _select(
        balancer,
        "node:1",
        filters=RouteCandidateFilters(required_context_window_tokens=64000, requires_image_multimodal=True),
    )

    assert lease.model_key == "m_big"

    # 没有任何成员合格时是 no_candidate（交给 preflight 失败处理），不是 no_capacity。
    blocked, reason = balancer.select(
        node_id="node:2",
        group_key="g1",
        route_index=0,
        filters=RouteCandidateFilters(required_context_window_tokens=400000),
    )
    assert blocked is None
    assert reason == "no_candidate"


def test_excluded_model_keys_are_skipped_within_a_request() -> None:
    balancer = _balancer(_group("g1", "m_a", "m_b"))

    lease = _select(balancer, "node:1", filters=RouteCandidateFilters(excluded_model_keys=frozenset({"m_a"})))

    assert lease.model_key == "m_b"


def test_group_member_budget_is_carried_by_lease() -> None:
    balancer = _balancer(_group("g1", "m_a", "m_b", max_rounds=3))

    lease = _select(balancer, "node:1")

    assert lease.max_retry_rounds == 3
    assert lease.passes_used == 0


def test_config_refresh_keeps_observations_and_exposes_new_member() -> None:
    permits = _FakePermits()
    clock = _Clock()
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=permits, clock=clock)

    first = _select(balancer, "node:1")
    balancer.record_request_start(first)
    balancer.release(first, outcome=LEASE_OUTCOME_SUCCESS)
    before = {row["model_key"]: row["rolling_rpm_60s"] for row in balancer.snapshot()["groups"]["g1"]["members"]}
    assert before["m_a"] == 1

    balancer.configure(groups={"g1": _group("g1", "m_a", "m_b", "m_c")}, config_revision=8)

    members = {row["model_key"] for row in balancer.snapshot()["groups"]["g1"]["members"]}
    assert members == {"m_a", "m_b", "m_c"}
    after = {row["model_key"]: row["rolling_rpm_60s"] for row in balancer.snapshot()["groups"]["g1"]["members"]}
    assert after["m_a"] == before["m_a"]


def test_config_refresh_unbinds_only_nodes_using_the_removed_member() -> None:
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=_FakePermits())

    node_a = _select(balancer, "node:a")
    node_b = _select(balancer, "node:b")
    assert {node_a.model_key, node_b.model_key} == {"m_a", "m_b"}

    # 无关配置的 revision 前进 + 组里加成员：既有绑定一律不动。
    balancer.configure(groups={"g1": _group("g1", "m_a", "m_b", "m_c")}, config_revision=99)
    assert balancer.bound_model_for_node("node:a") == "m_a"
    assert balancer.bound_model_for_node("node:b") == "m_b"

    # 只有绑定成员被移出组的那个节点才解绑。
    balancer.configure(groups={"g1": _group("g1", "m_b", "m_c")}, config_revision=100)
    assert balancer.bound_model_for_node("node:a") == ""
    assert balancer.bound_model_for_node("node:b") == "m_b"

    lease, reason = balancer.select(node_id="node:a", group_key="g1", route_index=0)
    assert reason == ""
    assert lease is not None and lease.model_key in {"m_b", "m_c"}


def test_member_removed_from_group_is_forgotten_but_active_lease_releases_cleanly() -> None:
    permits = _FakePermits()
    balancer = _balancer(_group("g1", "m_a", "m_b"), permits=permits)
    lease = _select(balancer, "node:1")

    balancer.configure(groups={"g1": _group("g1", "m_b")}, config_revision=7)
    balancer.release(lease, outcome=LEASE_OUTCOME_SUCCESS)

    assert permits.running.get("m_a", 0) == 0
    assert sum(row["reserved"] for row in balancer.snapshot()["groups"]["g1"]["members"]) == 0


def test_throttle_dimension_classification_is_best_effort() -> None:
    assert classify_throttle_dimension("Error code: 429 - rpm limit") == "rpm"
    assert classify_throttle_dimension("Error code: 429 - tpm limit exceeded") == "tpm"
    assert classify_throttle_dimension("Error code: 429 - rps") == "rps"
    assert classify_throttle_dimension("Error code: 429 - token quota") == "token"
    # 文本里没有维度线索时记 unknown，不影响它已经被判为 429 的事实。
    assert classify_throttle_dimension("Error code: 429 - too many requests") == "unknown"


def test_balancer_without_permit_source_still_binds() -> None:
    balancer = ModelLoadBalancer(permit_source=None, monotonic=_Clock())
    balancer.configure(groups={"g1": _group("g1", "m_a", "m_b")}, config_revision=1)

    lease, reason = balancer.select(node_id="node:1", group_key="g1", route_index=0)

    # embedded/web 模式下没有 controller：退化为无并发计数的绑定，不能报错。
    assert reason == ""
    assert lease is not None and lease.permit is None
    assert lease.local_capacity is None
    balancer.release(lease, outcome=LEASE_OUTCOME_SUCCESS)


def test_least_loaded_key_selection_breaks_fixed_key_order() -> None:
    controller = ModelKeyConcurrencyController(
        resolve_model_limits=lambda model_ref: {"key_indexes": [0, 1, 2], "per_key_limits": {0: 2, 1: 2, 2: 2}}
    )

    # 固定顺序会连拿 key0 两次；按负载取应摊到不同 key。
    first = controller.acquire_least_loaded(model_ref="m")
    second = controller.acquire_least_loaded(model_ref="m")
    third = controller.acquire_least_loaded(model_ref="m")

    assert {first.key_index, second.key_index, third.key_index} == {0, 1, 2}


def test_effective_capacity_reports_unlimited_as_none() -> None:
    controller = ModelKeyConcurrencyController(resolve_model_limits=lambda model_ref: {"key_indexes": [0], "per_key_limits": {0: None}})

    assert controller.effective_capacity("m") is None
    assert controller.try_acquire_first_available(model_ref="m") is not None

    limited = ModelKeyConcurrencyController(resolve_model_limits=lambda model_ref: {"key_indexes": [0, 1], "per_key_limits": {0: 2, 1: 3}})
    assert limited.effective_capacity("m") == 5
