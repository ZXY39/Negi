"""开机自启登记：谁写、写什么、读不到时怎么说。

钉住三件事：

* 命令形状 = ``negi_bootstrap.py start --no-browser``。走 start 是因为它先探端口，
  登录时不会把正在工作的实例收割重启；带 --no-browser 是因为登录不该弹页签。
* 容器与没有登记面的机器报"不支持 + 原因"，绝不假装成功，也不在系统里留下半条登记。
* 接口回的是重读一遍的登记事实，不是请求的那个值。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import g3ku.deployment.autostart as autostart
from main.api import bootstrap_rest

COMMAND = [r"C:\Negi\.venv\Scripts\pythonw.exe", r"C:\Negi\negi_bootstrap.py", "start", "--no-browser"]


class _FakeKey:
    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeWinreg:
    """够用的 HKCU Run 面：只实现这条车道真用到的四个动作，和一个可读失败开关。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deleted: list[str] = []
        self.error: OSError | None = None

    def OpenKey(self, _root, _path, _reserved, _access):  # noqa: N802 - winreg 的方法名
        if self.error:
            raise self.error
        return _FakeKey(self.values)

    def SetValueEx(self, key, name, _reserved, _type, value):  # noqa: N802
        key.store[name] = value

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in key.store:
            raise FileNotFoundError(name)
        return key.store[name], self.REG_SZ

    def DeleteValue(self, key, name):  # noqa: N802
        # 真 winreg 对不存在的值抛 FileNotFoundError，假件要一样，否则"删没了"会被测成"删成功"。
        if name not in key.store:
            raise FileNotFoundError(name)
        key.store.pop(name)
        self.deleted.append(name)


def test_launcher_command_uses_the_start_lane_without_a_browser():
    argv = autostart.launcher_command(root=Path(r"C:\Negi"))

    assert argv[1:] == [r"C:\Negi\negi_bootstrap.py", "start", "--no-browser"]


def test_windows_install_and_remove_the_run_value():
    fake = _FakeWinreg()
    backend = autostart._WindowsRegistryBackend(fake)

    backend.install(COMMAND)

    assert backend.is_installed(COMMAND) is True
    # 路径带空格时注册表里必须整段加引号，否则登录时这条值起不来。
    assert fake.values["Negi"].startswith('"C:\\Negi\\.venv\\Scripts\\pythonw.exe"')

    backend.uninstall()

    assert backend.is_installed(COMMAND) is False
    assert fake.deleted == ["Negi"]


def test_windows_uninstall_of_an_absent_value_is_not_a_failure():
    fake = _FakeWinreg()

    autostart._WindowsRegistryBackend(fake).uninstall()

    assert fake.deleted == []


def test_windows_read_failure_is_reported_instead_of_reading_as_off(monkeypatch):
    """注册表策略挡下来时不能说"已关闭"：那会把读不动说成操作员的决定。"""
    fake = _FakeWinreg()
    fake.error = PermissionError("blocked by policy")
    monkeypatch.setattr(autostart, "_backend_name", lambda: autostart._BACKEND_WINDOWS)

    state = autostart.describe(winreg_module=fake, command=COMMAND)

    assert state["supported"] is True
    assert state["installed"] is False
    assert "policy" in state["reason"]


def test_container_reports_unsupported_and_writes_nothing(monkeypatch):
    touched: list[str] = []
    monkeypatch.setattr(autostart, "container_detected", lambda: True)
    monkeypatch.setattr(
        autostart, "_build_backend", lambda backend, winreg_module: touched.append(backend) or object()
    )

    state = autostart.describe()
    applied = autostart.apply_enabled(True)

    assert state["supported"] is False
    assert state["backend"] == autostart._BACKEND_NONE
    assert "容器" in state["reason"]
    assert applied["ok"] is False
    assert touched == [], "没有登记面时不碰任何后端"


def test_systemd_unit_runs_the_same_start_lane(monkeypatch, tmp_path: Path):
    runs: list[list[str]] = []

    def fake_run(argv):
        runs.append(list(argv))
        return 0

    monkeypatch.setattr(autostart, "_run", fake_run)
    monkeypatch.setattr(autostart, "RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    autostart._SystemdUserBackend().install(
        [str(tmp_path / ".venv/bin/python"), str(tmp_path / "negi_bootstrap.py"), "start", "--no-browser"]
    )

    text = (tmp_path / ".config" / "systemd" / "user" / autostart.SYSTEMD_UNIT_NAME).read_text(encoding="utf-8")
    assert "ExecStart=" in text and "--no-browser" in text
    assert runs[0] == ["systemctl", "--user", "daemon-reload"]
    assert runs[1] == ["systemctl", "--user", "enable", autostart.SYSTEMD_UNIT_NAME]


def test_systemd_enable_failure_becomes_a_reason_not_a_silent_success(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(autostart, "_run", lambda argv: 1 if "enable" in argv else 0)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    state = autostart.apply_enabled(True, backend=autostart._BACKEND_SYSTEMD)

    assert state["ok"] is False
    assert "enable" in state["reason"]


def test_fresh_install_enables_but_never_rewrites_a_live_registration(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(
        autostart, "describe", lambda **kwargs: {"supported": True, "installed": False, "backend": "windows"}
    )
    monkeypatch.setattr(autostart, "apply_enabled", lambda enabled: calls.append(enabled) or {"ok": True})

    autostart.ensure_enabled_on_fresh_install()

    assert calls == [True]

    monkeypatch.setattr(
        autostart, "describe", lambda **kwargs: {"supported": True, "installed": True, "backend": "windows"}
    )
    calls.clear()

    state = autostart.ensure_enabled_on_fresh_install()

    assert calls == [], "已经有一条登记在时不替操作员重写它"
    assert state["installed"] is True


# ---------------------------------------------------------------------------
# 接口面
# ---------------------------------------------------------------------------


class _Stub:
    def __init__(self, *, unlocked: bool = True) -> None:
        self._unlocked = unlocked

    def is_unlocked(self) -> bool:
        return self._unlocked

    def status(self) -> dict[str, object]:
        return {"mode": "unlocked" if self._unlocked else "locked"}


def _client_for(stub: _Stub, monkeypatch) -> TestClient:
    monkeypatch.setattr(bootstrap_rest, "_service", lambda: stub)
    app = FastAPI()
    app.include_router(bootstrap_rest.router)
    return TestClient(app)


def test_autostart_get_reports_the_registration(monkeypatch):
    seen = []

    def fake_describe():
        seen.append(1)
        return {"supported": True, "backend": "windows", "installed": True, "command": COMMAND, "reason": ""}

    monkeypatch.setattr(bootstrap_rest, "describe_autostart", fake_describe)
    client = _client_for(_Stub(), monkeypatch)

    body = client.get("/bootstrap/autostart").json()

    assert seen == [1]
    assert body["item"]["installed"] is True


def test_autostart_post_returns_the_reread_fact_not_the_request(monkeypatch):
    def fake_apply(enabled):
        return {
            "supported": True,
            "backend": "windows",
            "installed": bool(enabled),
            "command": COMMAND,
            "reason": "",
            "ok": True,
        }

    monkeypatch.setattr(bootstrap_rest, "apply_autostart", fake_apply)
    client = _client_for(_Stub(), monkeypatch)

    off = client.post("/bootstrap/autostart", json={"enabled": False}).json()

    assert off["item"]["installed"] is False


def test_autostart_post_says_why_a_container_refuses(monkeypatch):
    monkeypatch.setattr(
        bootstrap_rest,
        "apply_autostart",
        lambda enabled: {
            "supported": False,
            "backend": "none",
            "installed": False,
            "command": COMMAND,
            "reason": "容器里的 web 与 worker 由入口脚本和容器重启策略负责，不在系统里登记自启。",
            "ok": False,
        },
    )
    client = _client_for(_Stub(), monkeypatch)

    response = client.post("/bootstrap/autostart", json={"enabled": True})

    assert response.status_code == 400
    assert "容器" in response.json()["detail"]


def test_autostart_needs_an_unlocked_project(monkeypatch):
    client = _client_for(_Stub(unlocked=False), monkeypatch)

    assert client.get("/bootstrap/autostart").status_code == 423
    assert client.post("/bootstrap/autostart", json={"enabled": True}).status_code == 423
