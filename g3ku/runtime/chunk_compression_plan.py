"""分块压缩的统一容量与切块规划（节点 / 会话 / 记忆三条车道共用）。

规则只有一条：单块容量由**约束**推导，不写目标次数。

    容量 = 声明窗口 − 每块信封（压缩指令自身） − 输出余量
    若该 (任务, 节点/会话) 已有实测请求跨度（provider 回执的 输入+输出 最大值），
    容量再被 `观测跨度 × 安全系数` 夹一刀——声明窗口可能虚高（实测记录写 390000、
    真实 262144），而 provider 超窗往往不报错，只有实测跨度是可信下界。

块数由贪心装箱（原子组不可分）自然落 0..上限；三种情况直接拒发，不发任何请求：
容量非正、某个原子组单独就超容量、块数超过上限。装箱拒因的落点各自不同——节点=预检
拒发可恢复暂停，会话=窗口超限错误。记忆车道只共用**容量**这一半：它没有可切的历史，
因此不装箱，也**不**套上面那道实测跨度夹刀（记忆批次是 append-only 的多轮循环，下一轮
必然把上一轮的 prompt、模型输出与工具结果一起重发，估算恒大于此前任何一轮的真实跨度，
夹刀等于让每一批都死在第 2 轮）；估算超过 声明窗口 − 输出余量 就带真实原因返回，由上层
按 `window_refusal` 停车。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 一块压缩请求里留给摘要输出与指令尾部的余量：按窗口 10% 取，封顶 24000。
# 写成固定值会把小窗口模型的容量压成非正（25000 窗口的模型根本切不出块），
# 而大窗口的绝对余量又没必要跟着窗口涨。
CHUNK_OUTPUT_HEADROOM_TOKENS = 24_000
CHUNK_OUTPUT_HEADROOM_RATIO = 0.1
CHUNK_OUTPUT_HEADROOM_MIN_TOKENS = 1_000
# 一次压缩最多切几块：超过即规模问题，拒掉而不是逐块白烧。
CHUNK_MAX_CHUNKS = 12
# 观测跨度的安全系数：留一成余量，因为实测跨度只是下界，真实窗口可能更大但不会更小。
CHUNK_OBSERVED_SAFETY_RATIO = 0.9

CAPACITY_SOURCE_WINDOW = 'declared_window'
CAPACITY_SOURCE_OBSERVED = 'observed_request_span'

REFUSAL_CAPACITY_NON_POSITIVE = 'capacity_non_positive'
REFUSAL_GROUP_OVER_CAPACITY = 'group_over_capacity'
REFUSAL_OVER_MAX_CHUNKS = 'over_max_chunks'


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    """可行的切块计划：`spans` 是每组序号的 [start, end) 区间，按输入顺序连续切。"""

    spans: list[tuple[int, int]]
    capacity_tokens: int
    capacity_source: str
    history_tokens: int
    max_group_tokens: int

    @property
    def chunk_count(self) -> int:
        return len(self.spans)


@dataclass(frozen=True, slots=True)
class ChunkPlanRefusal:
    kind: str
    reason: str
    capacity_tokens: int
    capacity_source: str
    planned_chunks: int
    history_tokens: int
    max_group_tokens: int


def _output_headroom_tokens(context_window_tokens: int) -> int:
    return min(
        CHUNK_OUTPUT_HEADROOM_TOKENS,
        max(CHUNK_OUTPUT_HEADROOM_MIN_TOKENS, int(int(context_window_tokens or 0) * CHUNK_OUTPUT_HEADROOM_RATIO)),
    )


def resolve_chunk_capacity(
    *,
    context_window_tokens: int,
    envelope_tokens: int = 0,
    observed_span_tokens: int = 0,
) -> tuple[int, str]:
    """单块容量与其来源；容量非正时返回 0（调用方据此拒发）。"""

    window = int(context_window_tokens or 0)
    capacity = window - int(envelope_tokens or 0) - _output_headroom_tokens(window)
    source = CAPACITY_SOURCE_WINDOW
    observed = int(observed_span_tokens or 0)
    if observed > 0:
        observed_cap = int(observed * CHUNK_OBSERVED_SAFETY_RATIO)
        if observed_cap < capacity:
            capacity = observed_cap
            source = CAPACITY_SOURCE_OBSERVED
    return max(0, capacity), source


COMPRESSION_MODE_NONE = 'none'
COMPRESSION_MODE_SINGLE = 'single'
COMPRESSION_MODE_CHUNKED = 'chunked'


@dataclass(frozen=True, slots=True)
class CompressionPass:
    """一次压缩该怎么做：**块数 0 就是不切**，1 就是完整压缩，>1 才是分块。

    三态由同一份装箱结果推出，不再有"单发不等式 vs 装箱容量"两套口径。`deferred_spans`
    是本轮不压、留给下一跳的最新组（超块数上限时）；`truncation_group_indexes` 是单独
    一颗就超容量的原子组，执行器先对它做截断摘要再装箱。`refusal` 只在容量非正时非空。
    """

    mode: str
    spans: list[tuple[int, int]]
    deferred_spans: list[tuple[int, int]]
    truncation_group_indexes: list[int]
    capacity_tokens: int
    capacity_source: str
    history_tokens: int
    max_group_tokens: int
    planned_chunks: int
    refusal: ChunkPlanRefusal | None = None

    @property
    def chunk_count(self) -> int:
        return len(self.spans)

    @property
    def deferred_count(self) -> int:
        return len(self.deferred_spans)


def _pack_groups(counts: list[int], capacity: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    carry = 0
    for index, tokens in enumerate(counts):
        if start < index and carry + tokens > capacity:
            spans.append((start, index))
            start = index
            carry = 0
        carry += tokens
    if start < len(counts):
        spans.append((start, len(counts)))
    return spans


def plan_compression_pass(
    group_token_counts: list[int],
    *,
    estimated_total_tokens: int,
    trigger_tokens: int,
    context_window_tokens: int,
    envelope_tokens: int = 0,
    observed_span_tokens: int = 0,
    max_chunks: int = CHUNK_MAX_CHUNKS,
) -> CompressionPass:
    """压缩的唯一决策口：够小不压、装得下完整压一次、装不下分块压、超上限留到下一跳。

    纯函数，输入只有"还没压的原子组"和"这一发落到的那颗窗口"，所以换模型、暂停恢复、
    进程重启之后重跑它就是重算——不需要任何"上次到哪了"的特例。
    """
    counts = [max(0, int(item or 0)) for item in list(group_token_counts or [])]
    history_tokens = sum(counts)
    max_group_tokens = max(counts) if counts else 0
    capacity, source = resolve_chunk_capacity(
        context_window_tokens=context_window_tokens,
        envelope_tokens=envelope_tokens,
        observed_span_tokens=observed_span_tokens,
    )
    limit = max(1, int(max_chunks or 1))

    def _pass(**kwargs: Any) -> CompressionPass:
        kwargs.setdefault('deferred_spans', [])
        kwargs.setdefault('truncation_group_indexes', [])
        return CompressionPass(
            capacity_tokens=capacity,
            capacity_source=source,
            history_tokens=history_tokens,
            max_group_tokens=max_group_tokens,
            **kwargs,
        )

    if capacity <= 0:
        # 窗口连一块信封都装不下：这是唯一还不能"分块解决"的情形，交回调用方按既有
        # 拒因处理（节点=可恢复暂停，会话=窗口超限）。
        return _pass(
            mode=COMPRESSION_MODE_NONE,
            spans=[],
            deferred_spans=[],
            truncation_group_indexes=[],
            planned_chunks=0,
            refusal=ChunkPlanRefusal(
                kind=REFUSAL_CAPACITY_NON_POSITIVE,
                reason=(
                    f"token compression skipped: per-chunk capacity is {capacity} tok "
                    f"(no room left for a chunk request) [窗口={int(context_window_tokens or 0)} "
                    f"信封={int(envelope_tokens or 0)} 观测跨度={int(observed_span_tokens or 0)} "
                    f"可压历史={history_tokens}]"
                ),
                capacity_tokens=capacity,
                capacity_source=source,
                planned_chunks=0,
                history_tokens=history_tokens,
                max_group_tokens=max_group_tokens,
            ),
        )
    if not counts or int(estimated_total_tokens or 0) < int(trigger_tokens or 0):
        return _pass(
            mode=COMPRESSION_MODE_NONE,
            spans=[],
            deferred_spans=[],
            truncation_group_indexes=[],
            planned_chunks=0,
        )
    spans = _pack_groups(counts, capacity)
    truncation = [index for index, tokens in enumerate(counts) if tokens > capacity]
    deferred: list[tuple[int, int]] = []
    if len(spans) > limit:
        # 超上限不再拒发：压最老的那些，剩下的下一跳重算时自然进窗口。一次 pass 只压
        # limit 块，代价是多一跳，而不是把整次压缩打成错误。
        deferred = spans[limit:]
        spans = spans[:limit]
    if not spans:
        return _pass(
            mode=COMPRESSION_MODE_NONE,
            spans=[],
            deferred_spans=[],
            truncation_group_indexes=truncation,
            planned_chunks=0,
        )
    return _pass(
        mode=COMPRESSION_MODE_SINGLE if len(spans) == 1 else COMPRESSION_MODE_CHUNKED,
        spans=spans,
        deferred_spans=deferred,
        truncation_group_indexes=truncation,
        planned_chunks=len(spans) + len(deferred),
    )


def plan_chunk_groups(
    group_token_counts: list[int],
    *,
    context_window_tokens: int,
    envelope_tokens: int = 0,
    observed_span_tokens: int = 0,
    max_chunks: int = CHUNK_MAX_CHUNKS,
) -> ChunkPlan | ChunkPlanRefusal:
    """按原子组装边量切块；不可行时返回带 kind 的拒因（一次都不发）。"""

    counts = [max(0, int(item or 0)) for item in list(group_token_counts or [])]
    history_tokens = sum(counts)
    max_group_tokens = max(counts) if counts else 0
    capacity, source = resolve_chunk_capacity(
        context_window_tokens=context_window_tokens,
        envelope_tokens=envelope_tokens,
        observed_span_tokens=observed_span_tokens,
    )
    limit = max(1, int(max_chunks or 1))
    window = int(context_window_tokens or 0)
    context = (
        f"窗口={window} 信封={int(envelope_tokens or 0)} 输出余量={_output_headroom_tokens(window)} "
        f"观测跨度={int(observed_span_tokens or 0)} 可压历史={history_tokens}"
    )

    if capacity <= 0:
        return ChunkPlanRefusal(
            kind=REFUSAL_CAPACITY_NON_POSITIVE,
            reason=(
                f"token compression skipped: per-chunk capacity is {capacity} tok "
                f"(no room left for a chunk request) [{context}]"
            ),
            capacity_tokens=capacity,
            capacity_source=source,
            planned_chunks=0,
            history_tokens=history_tokens,
            max_group_tokens=max_group_tokens,
        )

    spans: list[tuple[int, int]] = []
    start = 0
    carry = 0
    for index, tokens in enumerate(counts):
        if start < index and carry + tokens > capacity:
            spans.append((start, index))
            start = index
            carry = 0
        carry += tokens
    if start < len(counts):
        spans.append((start, len(counts)))

    if max_group_tokens > capacity:
        return ChunkPlanRefusal(
            kind=REFUSAL_GROUP_OVER_CAPACITY,
            reason=(
                f"token compression skipped: an atomic group of {max_group_tokens} tok exceeds the "
                f"{capacity} tok per-chunk capacity (groups are indivisible) [{context}]"
            ),
            capacity_tokens=capacity,
            capacity_source=source,
            planned_chunks=len(spans),
            history_tokens=history_tokens,
            max_group_tokens=max_group_tokens,
        )
    if len(spans) > limit:
        return ChunkPlanRefusal(
            kind=REFUSAL_OVER_MAX_CHUNKS,
            reason=(
                f"token compression skipped: compressible history {history_tokens} tok needs {len(spans)} "
                f"chunks over the {capacity} tok per-chunk capacity (max {limit} chunks) [{context}]"
            ),
            capacity_tokens=capacity,
            capacity_source=source,
            planned_chunks=len(spans),
            history_tokens=history_tokens,
            max_group_tokens=max_group_tokens,
        )
    if not spans:
        return ChunkPlanRefusal(
            kind=REFUSAL_CAPACITY_NON_POSITIVE,
            reason=f"token compression skipped: compressible history is empty [{context}]",
            capacity_tokens=capacity,
            capacity_source=source,
            planned_chunks=0,
            history_tokens=history_tokens,
            max_group_tokens=max_group_tokens,
        )
    return ChunkPlan(
        spans=spans,
        capacity_tokens=capacity,
        capacity_source=source,
        history_tokens=history_tokens,
        max_group_tokens=max_group_tokens,
    )
