from __future__ import annotations

from main.runtime.model_load_balancer import QUOTA_BUCKET_CACHE_TTL_SECONDS, ModelLoadBalancer
from main.runtime.model_route import ResolvedLoadBalanceGroup, RouteMemberView


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class _Resolver:
    """记录每次被点名的绑定，用来数「一次 snapshot 真去解析了几回」。"""

    def __init__(self, buckets: dict[str, list[str]] | None = None) -> None:
        self.calls: list[str] = []
        self.buckets = dict(buckets or {})

    def __call__(self, model_key: str) -> list[str]:
        self.calls.append(str(model_key))
        return list(self.buckets.get(str(model_key)) or [])


def _group(*keys: str) -> ResolvedLoadBalanceGroup:
    return ResolvedLoadBalanceGroup(
        group_key='g1',
        enabled=True,
        max_retry_rounds=1,
        members=[
            RouteMemberView(
                model_key=key,
                enabled=True,
                context_window_tokens=200000,
                image_multimodal_enabled=False,
            )
            for key in keys
        ],
    )


def _balancer(resolver: _Resolver, clock: _Clock) -> ModelLoadBalancer:
    balancer = ModelLoadBalancer(resolve_quota_buckets=resolver, monotonic=clock)
    balancer.configure(groups={'g1': _group('m_a', 'm_b')}, config_revision=1)
    return balancer


def _bucket_indices(snapshot: dict) -> list[int]:
    members = snapshot['groups']['g1']['members']
    return [int(member['quota_bucket_index']) for member in members]


def test_repeated_snapshot_resolves_each_member_once() -> None:
    clock = _Clock()
    resolver = _Resolver({'m_a': ['bkt:a'], 'm_b': ['bkt:b']})
    balancer = _balancer(resolver, clock)

    first = _bucket_indices(balancer.snapshot())
    calls_after_first = len(resolver.calls)
    assert calls_after_first == 2

    for _ in range(5):
        assert _bucket_indices(balancer.snapshot()) == first
    assert len(resolver.calls) == calls_after_first


def test_rebind_clears_bucket_cache() -> None:
    clock = _Clock()
    resolver = _Resolver({'m_a': ['bkt:a'], 'm_b': ['bkt:b']})
    balancer = _balancer(resolver, clock)
    balancer.snapshot()
    calls = len(resolver.calls)

    balancer.configure(groups={'g1': _group('m_a', 'm_b')}, config_revision=2)
    balancer.snapshot()
    assert len(resolver.calls) > calls


def test_bucket_cache_ages_out_after_ttl() -> None:
    clock = _Clock()
    resolver = _Resolver({'m_a': ['bkt:a'], 'm_b': ['bkt:b']})
    balancer = _balancer(resolver, clock)
    balancer.snapshot()
    calls = len(resolver.calls)

    clock.advance(QUOTA_BUCKET_CACHE_TTL_SECONDS * 2)
    balancer.snapshot()
    assert len(resolver.calls) > calls


def test_unresolved_identity_is_not_cached() -> None:
    """未解锁时解析不到密钥材料，不能把这个状态冻在缓存里等 TTL。"""

    clock = _Clock()
    resolver = _Resolver()
    balancer = _balancer(resolver, clock)

    first = _bucket_indices(balancer.snapshot())
    calls_after_first = len(resolver.calls)
    second = _bucket_indices(balancer.snapshot())

    assert first == [-1, -1]
    assert second == first
    assert len(resolver.calls) > calls_after_first
