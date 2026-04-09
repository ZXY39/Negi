from __future__ import annotations

from types import SimpleNamespace

import g3ku.web.main as web_main


def test_run_server_uses_app_object_when_reload_is_disabled(monkeypatch):
    captured: dict[str, object] = {}

    class _FakeServer:
        def __init__(self, config):
            captured["config_app"] = getattr(config, "app", None)

        def run(self):
            captured["ran"] = True

    def _fake_config(app_value, **kwargs):
        captured["config_kwargs"] = dict(kwargs)
        return SimpleNamespace(app=app_value)

    monkeypatch.setattr(web_main, "_install_process_shutdown_hooks", lambda: None)
    monkeypatch.setattr(web_main, "set_server_instance", lambda server: captured.setdefault("server_instances", []).append(server))
    monkeypatch.setattr(web_main.uvicorn, "Config", _fake_config)
    monkeypatch.setattr(web_main.uvicorn, "Server", _FakeServer)

    web_main.run_server(host="127.0.0.1", port=18790, reload=False, log_level="info")

    assert captured["config_app"] is web_main.app
    assert captured["config_kwargs"] == {
        "host": "127.0.0.1",
        "port": 18790,
        "reload": False,
        "log_level": "info",
    }
    assert captured["ran"] is True


def test_run_server_keeps_import_string_when_reload_is_enabled(monkeypatch):
    captured: dict[str, object] = {}

    monkeypatch.setattr(web_main, "_install_process_shutdown_hooks", lambda: None)
    monkeypatch.setattr(
        web_main.uvicorn,
        "run",
        lambda app_value, **kwargs: captured.update({"app_value": app_value, "kwargs": dict(kwargs)}),
    )

    web_main.run_server(host="127.0.0.1", port=18790, reload=True, log_level="debug")

    assert captured["app_value"] == "g3ku.web.main:app"
    assert captured["kwargs"] == {
        "host": "127.0.0.1",
        "port": 18790,
        "reload": True,
        "log_level": "debug",
    }
