import os
import sys
from types import SimpleNamespace

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from g3ku.shells import tray


def test_open_ui_opens_the_loopback_url(monkeypatch):
    opened = []
    monkeypatch.setattr(tray.webbrowser, "open", lambda url: opened.append(url) or True)
    tray._open_ui(18790)
    assert opened == ["http://127.0.0.1:18790/"]


def test_exit_confirm_text_names_running_work_counts():
    snapshot = {"has_running_work": True, "running_sessions": ["a", "b"], "running_tasks": ["t"]}
    text = tray._exit_confirm_text(snapshot)
    assert "2" in text and "1" in text
    assert "退出" in text


def test_exit_confirm_text_idle_has_no_counts():
    text = tray._exit_confirm_text({"has_running_work": False, "running_sessions": [], "running_tasks": []})
    assert "退出" in text


def test_exit_action_confirmed_requests_exit_with_pause(monkeypatch):
    posted = {}
    monkeypatch.setattr(tray, "_check_running_work", lambda port: {"has_running_work": False})
    monkeypatch.setattr(tray, "_confirm_exit", lambda snapshot: True)
    monkeypatch.setattr(tray, "_request_exit", lambda port: posted.update(port=port) or True)
    tray._exit_action(18790)
    assert posted == {"port": 18790}


def test_exit_action_cancelled_does_not_request_exit(monkeypatch):
    called = []
    monkeypatch.setattr(tray, "_check_running_work", lambda port: {"has_running_work": True})
    monkeypatch.setattr(tray, "_confirm_exit", lambda snapshot: False)
    monkeypatch.setattr(tray, "_request_exit", lambda port: called.append(port) or True)
    tray._exit_action(18790)
    assert called == []


def test_request_exit_posts_pause_running_work(monkeypatch):
    seen = {}

    class _Resp:
        status = 200

        def read(self):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        seen["url"] = req.full_url
        seen["body"] = req.data
        seen["method"] = req.get_method()
        return _Resp()

    monkeypatch.setattr(tray._OPENER, "open", _open)
    assert tray._request_exit(18790) is True
    assert seen["method"] == "POST"
    assert seen["url"].endswith("/api/bootstrap/exit")
    assert b'"pause_running_work": true' in seen["body"] or b'"pause_running_work":true' in seen["body"]


def test_watchdog_stops_icon_after_consecutive_misses(monkeypatch):
    monkeypatch.setattr(tray, "_service_alive", lambda port: False)
    monkeypatch.setattr(tray, "STATUS_POLL_SECONDS", 0.01)
    stopped = []
    icon = SimpleNamespace(stop=lambda: stopped.append(True))
    import threading

    tray._watch_service(18790, icon, threading.Event(), miss_limit=3)
    assert stopped == [True]


def test_watchdog_keeps_icon_while_service_alive(monkeypatch):
    monkeypatch.setattr(tray, "_service_alive", lambda port: True)
    monkeypatch.setattr(tray, "STATUS_POLL_SECONDS", 0.01)
    stopped = []
    icon = SimpleNamespace(stop=lambda: stopped.append(True))
    import threading

    stop = threading.Event()

    def _stop_after_a_bit():
        import time

        time.sleep(0.05)
        stop.set()

    threading.Thread(target=_stop_after_a_bit, daemon=True).start()
    tray._watch_service(18790, icon, stop, miss_limit=3)
    assert stopped == []


def test_run_tray_fail_closed_without_pystray(monkeypatch):
    monkeypatch.setattr(tray, "_TRAY_AVAILABLE", False)
    monkeypatch.setattr(tray.os, "name", "nt")
    assert tray.run_tray(18790) == 0


def test_run_tray_non_windows_returns_zero(monkeypatch):
    monkeypatch.setattr(tray.os, "name", "posix")
    assert tray.run_tray(18790) == 0
