from __future__ import annotations

from main.runtime.model_load_balancer import (
    PENALTY_HALF_LIFE_SECONDS,
    REMOTE_UPSTREAM_VIEW_STALE_SECONDS,
)
from main.runtime.model_route import LEASE_OUTCOME_SUCCESS
from tests.resources.test_model_load_balancer import (
    _balancer,
    _Clock,
    _group,
    _select,
)


def test_export_buffer_gated_by_share_flag() -> None:
    clock = _Clock()
    balancer = _balancer(_group("g1", "m_a"), clock=clock, buckets={"m_a": ["bucketA"]})

    lease = _select(balancer, "node:1")
    balancer.record_request_start(lease)
    balancer.record_outcome(lease, status_code=429, error_text="429 rpm limit")
    # 未开启共享：不白攒出站事件。
    assert balancer.drain_outbound_events() == []

    balancer.set_share_observation(True)
    lease2 = _select(balancer, "node:2")
    balancer.record_request_start(lease2)
    balancer.record_outcome(lease2, status_code=429, error_text="429 rpm limit")
    events = balancer.drain_outbound_events()
    kinds = sorted(event["kind"] for event in events)
    assert kinds == ["start", "throttle"]
    assert all(event["bucket_key"] == "bucketA" for event in events)
    assert all(event["ts"] == clock.now for event in events)
    assert len({event["id"] for event in events}) == 2
    # drain 即清。
    assert balancer.drain_outbound_events() == []


def test_worker_ingests_web_events_and_dedupes() -> None:
    clock = _Clock()
    web = _balancer(_group("g1", "m_a"), clock=clock, buckets={"m_a": ["bucketA"]})
    worker = _balancer(_group("g1", "m_a"), clock=clock, buckets={"m_a": ["bucketA"]})
    web.set_share_observation(True)

    lease = _select(web, "node:1")
    web.record_request_start(lease)
    web.record_outcome(lease, status_code=429, error_text="429 rpm limit")
    web.release(lease, outcome=LEASE_OUTCOME_SUCCESS)
    events = web.drain_outbound_events()

    assert worker.ingest_remote_events(events) == 2
    view = worker.upstream_view()
    assert view["m_a"]["rolling_rpm_60s"] == 1
    assert view["m_a"]["penalty_429"] > 0.5
    # 与 web 本地算出的同一份观测一致（同墙钟、同桶）。
    web_member = web.snapshot()["groups"]["g1"]["members"][0]
    assert web_member["rolling_rpm_60s"] == 1
    assert abs(web_member["penalty_429"] - view["m_a"]["penalty_429"]) < 1e-6

    # 重投同一批事件：幂等，不重复计。
    assert worker.ingest_remote_events(events) == 0
    assert worker.upstream_view()["m_a"]["rolling_rpm_60s"] == 1


def test_remote_view_overrides_local_and_falls_back_when_stale() -> None:
    clock = _Clock()
    web = _balancer(_group("g1", "m_a"), clock=clock, buckets={"m_a": ["bucketA"]})
    # web 本地零事件，但收到 worker 合并视图：打分读合并值。
    web.set_remote_upstream_view({"m_a": {"rolling_rpm_60s": 10, "penalty_429": 2.0}})

    lease = _select(web, "node:1")
    assert lease.rolling_rpm == 10
    assert lease.penalty_before == 2.0

    # 视图陈旧（worker 离线）：退回本地观测，此时本地零事件 => 0。
    clock.advance(REMOTE_UPSTREAM_VIEW_STALE_SECONDS + 1)
    lease2 = _select(web, "node:2")
    assert lease2.rolling_rpm == 0
    assert lease2.penalty_before == 0.0


def test_remote_view_decay_uses_wall_clock_consistently() -> None:
    clock = _Clock()
    worker = _balancer(_group("g1", "m_a"), clock=clock, buckets={"m_a": ["bucketA"]})
    web = _balancer(_group("g1", "m_a"), clock=clock, buckets={"m_a": ["bucketA"]})
    web.set_share_observation(True)

    lease = _select(web, "node:1")
    web.record_outcome(lease, status_code=429, error_text="429 rpm limit")
    worker.ingest_remote_events(web.drain_outbound_events())
    assert worker.upstream_view()["m_a"]["penalty_429"] > 0.5

    # 墙钟推进超过保留窗：惩罚衰减到近零（跨进程同口径）。
    clock.advance(PENALTY_HALF_LIFE_SECONDS * 7)
    assert worker.upstream_view()["m_a"]["penalty_429"] < 0.01


def test_upstream_view_dedupes_model_key_across_groups() -> None:
    clock = _Clock()
    # 同一颗 m_a 挂在两个组里：广播视图按 model_key 去重，不翻倍。
    worker = _balancer(
        _group("g1", "m_a", "m_b"),
        _group("g2", "m_a", "m_c"),
        clock=clock,
        buckets={"m_a": ["bucketA"], "m_b": ["bucketB"], "m_c": ["bucketC"]},
    )
    view = worker.upstream_view()
    assert set(view) == {"m_a", "m_b", "m_c"}


def test_share_disabled_keeps_node_selection_unchanged() -> None:
    clock = _Clock()
    balancer = _balancer(_group("g1", "m_a", "m_b"), clock=clock)
    # 默认不开共享、无合并视图：选择行为与改造前一致（摊平、不热点）。
    bound = [_select(balancer, f"node:{i}").model_key for i in range(20)]
    counts = {key: bound.count(key) for key in ("m_a", "m_b")}
    assert max(counts.values()) - min(counts.values()) <= 1
    assert balancer.drain_outbound_events() == []
