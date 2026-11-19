import asyncio
import os
import sys
import threading
import types

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from g3ku.utils import iocp_guard


def test_guarded_poll_swallows_invalid_state_only():
    def raising_poll(self, timeout=None):
        raise asyncio.InvalidStateError("invalid state")

    guarded = iocp_guard._make_guarded_poll(raising_poll)
    assert guarded(object(), None) is None

    def ok_poll(self, timeout=None):
        return "sentinel"

    assert iocp_guard._make_guarded_poll(ok_poll)(object(), None) == "sentinel"

    def other_error(self, timeout=None):
        raise RuntimeError("unrelated")

    with pytest.raises(RuntimeError):
        iocp_guard._make_guarded_poll(other_error)(object(), None)


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("G3KU_IOCP_GUARD", "0")
    assert iocp_guard.install_iocp_hardening() is False


@pytest.mark.skipif(os.name != "nt", reason="Windows IOCP only")
def test_install_is_idempotent():
    assert iocp_guard.install_iocp_hardening() is True
    assert iocp_guard.install_iocp_hardening() is True
    from asyncio import windows_events

    assert getattr(windows_events.IocpProactor, "_g3ku_poll_guarded", False)
    assert getattr(windows_events._OverlappedFuture, "_g3ku_thread_audited", False)


@pytest.mark.skipif(os.name != "nt", reason="Windows IOCP only")
def test_audit_flags_cross_thread_set_result(monkeypatch):
    iocp_guard.install_iocp_hardening()
    records: list[str] = []
    sink_id = iocp_guard.logger.add(lambda msg: records.append(str(msg)), level="WARNING")
    try:
        from asyncio import windows_events

        loop = asyncio.new_event_loop()
        started = threading.Event()

        def run_loop():
            asyncio.set_event_loop(loop)
            started.set()
            loop.run_forever()

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()
        started.wait(5)
        try:
            ov = types.SimpleNamespace(cancel=lambda: None, address=0, pending=False)
            fut = windows_events._OverlappedFuture(ov, loop=loop)
            fut.set_result(1)  # 本线程不是 loop 线程 → 必须被审计点名
            assert any("cross-thread future mutation" in r for r in records)
            assert any("set_result" in r for r in records)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            t.join(5)
            loop.close()
    finally:
        iocp_guard.logger.remove(sink_id)
