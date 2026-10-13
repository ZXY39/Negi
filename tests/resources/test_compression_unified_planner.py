"""压缩的唯一决策口：块数 0 就是不切，1 就是完整压缩，>1 才是分块。

三态由同一份装箱结果推出。之前是两套口径：`single_shot_tokens > window` 这条不等式决定
单发还是分块，而装箱容量另算（窗口−信封−余量，再被实测跨度夹一刀），两者对不上时就会出现
"该分块却整发"或"装得下却切了"。这里钉住新合同。
"""

from __future__ import annotations

from g3ku.runtime.chunk_compression_plan import (
    COMPRESSION_MODE_CHUNKED,
    COMPRESSION_MODE_NONE,
    COMPRESSION_MODE_SINGLE,
    REFUSAL_CAPACITY_NON_POSITIVE,
    plan_compression_pass,
)

_WINDOW = 400_000
_ENVELOPE = 2_000


def _pass(groups, *, estimate, trigger=_WINDOW, window=_WINDOW, envelope=_ENVELOPE, observed=0, max_chunks=12):
    return plan_compression_pass(
        groups,
        estimated_total_tokens=estimate,
        trigger_tokens=trigger,
        context_window_tokens=window,
        envelope_tokens=envelope,
        observed_span_tokens=observed,
        max_chunks=max_chunks,
    )


def test_small_enough_means_no_compression_at_all() -> None:
    planned = _pass([10_000, 10_000], estimate=20_000, trigger=300_000)

    assert planned.mode == COMPRESSION_MODE_NONE
    assert planned.chunk_count == 0 and planned.deferred_count == 0
    assert planned.refusal is None


def test_history_that_fits_one_chunk_is_a_single_complete_pass() -> None:
    planned = _pass([30_000, 40_000], estimate=380_000, trigger=300_000)

    assert planned.mode == COMPRESSION_MODE_SINGLE
    assert planned.spans == [(0, 2)]


def test_history_over_capacity_splits_and_keeps_every_group_in_order() -> None:
    planned = _pass([200_000, 200_000, 200_000], estimate=640_000, trigger=300_000)

    assert planned.mode == COMPRESSION_MODE_CHUNKED
    assert planned.chunk_count >= 3
    assert [index for span in planned.spans for index in range(span[0], span[1])] == [0, 1, 2]


def test_over_chunk_ceiling_defers_the_newest_groups_instead_of_refusing() -> None:
    planned = _pass([60_000] * 20, estimate=1_300_000, trigger=300_000, max_chunks=3)

    assert planned.mode == COMPRESSION_MODE_CHUNKED
    assert planned.chunk_count == 3
    assert planned.deferred_count > 0, "超上限要留到下一跳重算，而不是整次压缩拒发"
    assert planned.refusal is None


def test_single_oversized_group_is_reported_for_truncation_not_refused() -> None:
    planned = _pass([500_000, 10_000], estimate=600_000, trigger=300_000)

    assert planned.truncation_group_indexes == [0]
    assert planned.refusal is None, "单组超容量的处置是截断摘要，不是报错"


def test_only_a_non_positive_capacity_still_refuses() -> None:
    planned = _pass([10_000], estimate=50_000, trigger=10_000, window=1_500, envelope=2_000)

    assert planned.mode == COMPRESSION_MODE_NONE
    assert planned.refusal is not None
    assert planned.refusal.kind == REFUSAL_CAPACITY_NON_POSITIVE


def test_a_bigger_model_window_repacks_the_same_history_into_fewer_chunks() -> None:
    """换模型不需要任何"续跑"特例：规划器只吃（剩余组, 窗口），重跑就是重算。"""
    groups = [150_000, 150_000, 150_000, 150_000]
    tight = _pass(groups, estimate=700_000, trigger=300_000, window=400_000)
    wide = _pass(groups, estimate=700_000, trigger=600_000, window=1_000_000, envelope=_ENVELOPE)

    assert tight.mode == COMPRESSION_MODE_CHUNKED and tight.chunk_count > wide.chunk_count
    assert wide.mode == COMPRESSION_MODE_SINGLE and wide.chunk_count == 1


def test_observed_span_clamps_capacity_below_the_declared_window() -> None:
    """声明窗口可以虚高（实测 262,144 对声明 390,000）：容量按实测跨度夹一刀。"""
    declared = _pass([100_000] * 6, estimate=700_000, trigger=300_000, window=390_000)
    clamped = _pass(
        [100_000] * 6,
        estimate=700_000,
        trigger=300_000,
        window=390_000,
        observed=120_000,
    )

    assert clamped.capacity_tokens < declared.capacity_tokens
    assert clamped.capacity_source == "observed_request_span"
    assert clamped.chunk_count > declared.chunk_count
