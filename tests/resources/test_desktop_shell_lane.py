import os
import socket
import subprocess
import sys
from types import SimpleNamespace

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

import negi_bootstrap
from g3ku.shells import desktop


def test_shell_url_prefers_the_explicit_port_then_config(monkeypatch) -> None:
    monkeypatch.setattr(desktop, "shell_url", desktop.shell_url)
    assert desktop.shell_url(9123) == "http://127.0.0.1:9123/"

    def _boom():
        raise RuntimeError("no config on this box")

    import g3ku.config.loader as loader

    monkeypatch.setattr(loader, "load_config", _boom)
    assert desktop.shell_url() == "http://127.0.0.1:18790/"


def test_server_reachable_reads_the_live_endpoint() -> None:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert desktop.server_reachable(f"http://127.0.0.1:{port}/") is False


def test_import_error_names_the_missing_piece_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    message = desktop.import_error()
    assert "Windows" in message


class _PermissionSignal:
    def __init__(self, kind: str) -> None:
        self.PermissionKind = kind
        self.State = 2


def test_microphone_hook_allows_only_the_microphone() -> None:
    handlers = []

    class Core:
        def __init__(self) -> None:
            self.PermissionRequested = self

        def __iadd__(self, handler):
            handlers.append(handler)
            return self

    window = SimpleNamespace(native=SimpleNamespace(webview=SimpleNamespace(CoreWebView2=Core())))

    note = desktop._attach_microphone_permission(window)
    assert "麦克风" in note
    assert len(handlers) == 1

    mic = _PermissionSignal("Microphone")
    handlers[0](None, mic)
    assert mic.State == desktop.PERMISSION_ALLOWED

    other = _PermissionSignal("Sensors")
    handlers[0](None, other)
    assert other.State == 2


def test_microphone_hook_reports_a_readable_failure_when_the_handle_is_gone() -> None:
    class Broken:
        @property
        def CoreWebView2(self):
            raise RuntimeError("not on the UI thread")

    window = SimpleNamespace(native=SimpleNamespace(webview=Broken()))
    note = desktop._attach_microphone_permission(window)
    assert "权限回调" in note


def test_start_window_flag_is_consumed_and_opens_the_shell(monkeypatch) -> None:
    opened = {"browser": [], "shell": []}
    monkeypatch.setattr(negi_bootstrap, "_load_bootstrap_config", lambda: {"web": {"port": 18790}})
    monkeypatch.setattr(negi_bootstrap, "_probe_web_ready", lambda port: True)
    monkeypatch.setattr(negi_bootstrap, "_open_browser", lambda url: opened["browser"].append(url) or True)
    monkeypatch.setattr(negi_bootstrap, "_open_desktop_window", lambda port: opened["shell"].append(port) or True)

    assert negi_bootstrap._run_start(["--window"]) == 0
    assert opened["shell"] == [18790]
    assert opened["browser"] == []

    opened["shell"].clear()
    assert negi_bootstrap._run_start([]) == 0
    assert opened["shell"] == []
    assert opened["browser"] == ["http://127.0.0.1:18790/"]


def test_cold_start_with_window_opens_the_shell_not_the_browser(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(negi_bootstrap, "_load_bootstrap_config", lambda: {})
    monkeypatch.setattr(negi_bootstrap, "_probe_web_ready", lambda port: False)
    monkeypatch.setattr(negi_bootstrap, "_open_runtime_console_log_stream", lambda: None)
    monkeypatch.setattr(negi_bootstrap, "_wait_for_web_ready", lambda child, port: True)
    monkeypatch.setattr(
        negi_bootstrap,
        "_spawn_web_runtime",
        lambda args, **kwargs: (seen.update(args=list(args)), SimpleNamespace(returncode=None, poll=lambda: None, wait=lambda: 0))[1],
    )
    monkeypatch.setattr(negi_bootstrap, "_open_browser", lambda url: seen.update(browser=url) or True)
    monkeypatch.setattr(negi_bootstrap, "_open_desktop_window", lambda port: seen.update(shell=port) or True)

    assert negi_bootstrap._run_start(["--window", "--hidden", "--port", "9555"]) == 0
    assert seen["args"] == ["web", "--port", "9555"]
    assert seen["shell"] == 9555
    assert "browser" not in seen


def test_desktop_window_goes_out_through_pythonw_detached(monkeypatch, tmp_path) -> None:
    captured = {}

    class _Popen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs

    pythonw = tmp_path / ".venv" / "Scripts" / "pythonw.exe"
    pythonw.parent.mkdir(parents=True)
    pythonw.write_bytes(b"")
    monkeypatch.setattr(negi_bootstrap, "VENV_PYTHON", tmp_path / ".venv" / "Scripts" / "python.exe")
    monkeypatch.setattr(negi_bootstrap.subprocess, "Popen", _Popen)

    assert negi_bootstrap._open_desktop_window(9555) is True
    assert captured["argv"][1:4] == ["-m", "g3ku", "window"]
    assert captured["argv"][4:] == ["--port", "9555"]
    if os.name == "nt":
        assert captured["kwargs"]["creationflags"] == (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )


def test_desktop_window_falls_back_to_the_browser_without_pythonw(monkeypatch, tmp_path) -> None:
    opened = []
    monkeypatch.setattr(negi_bootstrap, "VENV_PYTHON", tmp_path / ".venv" / "Scripts" / "python.exe")
    monkeypatch.setattr(negi_bootstrap, "_open_browser", lambda url: opened.append(url) or True)

    assert negi_bootstrap._open_desktop_window(18790) is True
    assert opened == ["http://127.0.0.1:18790/"]
