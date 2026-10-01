from __future__ import annotations

from types import SimpleNamespace

from main.service.runtime_service import MainRuntimeService


class _Conn:
    def __init__(self, *, available: bool) -> None:
        self._available = available

    def is_available(self) -> bool:
        return self._available


def _snapshot_for(connections) -> dict[str, float]:
    service = SimpleNamespace(_callback_client=SimpleNamespace(_transport=SimpleNamespace(_pool=SimpleNamespace(connections=connections))))
    return MainRuntimeService._callback_pool_snapshot(service)


def test_pool_snapshot_separates_leased_from_available() -> None:
    """分岔判据：空闲未修剪（available）与被借走没还（not available）必须分成两个数。"""
    stats = _snapshot_for([_Conn(available=True), _Conn(available=False), _Conn(available=False), _Conn(available=True)])

    assert stats['loopback_pool_connections'] == 4.0
    assert stats['loopback_pool_leased'] == 2.0


def test_pool_snapshot_reports_minus_one_before_the_client_exists() -> None:
    """客户端还没建过时不许报 0——那会被读成"池是空的"。"""
    service = SimpleNamespace(_callback_client=None)

    stats = MainRuntimeService._callback_pool_snapshot(service)

    assert stats == {'loopback_pool_connections': -1.0, 'loopback_pool_leased': -1.0}


def test_pool_snapshot_survives_a_connection_that_refuses_to_answer() -> None:
    stats = _snapshot_for([_Conn(available=True), object(), _Conn(available=False)])

    assert stats['loopback_pool_connections'] == 3.0
    assert stats['loopback_pool_leased'] == 1.0
