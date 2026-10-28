"""安装计划与执行状态机（P1b）。

计划是纯函数，执行用替身步骤验状态机；真安装要在目标机器上跑，属于上机验收的一部分。
"""

from __future__ import annotations

import platform as platform_module

import pytest

import main  # noqa: F401  (production import order)
from g3ku.sandbox import installer
from g3ku.sandbox.installer import InstallPlan, InstallStep, build_install_plan


@pytest.fixture(autouse=True)
def _reset_state():
    installer.reset_install_state_for_tests()
    yield
    installer.reset_install_state_for_tests()


def _reading(**overrides):
    payload = {
        "state": "not_installed",
        "backend": "",
        "reason": "",
        "enforcement": {},
        "detail": {},
        "platform": "windows",
    }
    payload.update(overrides)
    return payload


def test_ready_has_nothing_to_install() -> None:
    plan = build_install_plan(_reading(state="ready", backend="bwrap"))
    assert plan.steps == []
    assert plan.installable is False
    assert "已就绪" in plan.notes[0]


def test_linux_plan_uses_package_manager_and_elevation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "linux")
    which = {"apt-get": "/usr/bin/apt-get", "pkexec": "/usr/bin/pkexec"}
    monkeypatch.setattr(installer.shutil, "which", lambda name: which.get(name))
    plan = build_install_plan(_reading(state="runtime_missing", platform="linux"))
    assert plan.installable is True
    assert plan.requires_elevation is True
    step = plan.steps[0]
    assert step.id == "install-bubblewrap"
    assert step.command[:2] == ["pkexec", "apt-get"]
    assert step.command[-1] == "bubblewrap"
    assert any("remove bubblewrap" in item for item in plan.uninstall_commands)


def test_linux_without_package_manager_is_not_installable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "linux")
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    plan = build_install_plan(_reading(state="not_installed", platform="linux"))
    assert plan.installable is False
    assert plan.reason == "no_supported_package_manager"


def test_windows_without_distro_asks_for_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "windows")
    plan = build_install_plan(_reading(detail={"wsl": "wsl.exe", "distros": []}))
    assert plan.installable is False
    assert plan.reason == "wsl_distro_missing"
    assert any("wsl --install" in note for note in plan.notes)
    step = plan.steps[0]
    assert step.manual is True
    assert step.command == ["wsl.exe", "--install", "-d", "Ubuntu"]


def test_windows_with_distro_installs_bwrap_inside_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "windows")
    plan = build_install_plan(
        _reading(detail={"wsl": "C:/Windows/System32/wsl.exe", "distros": ["Ubuntu", "Debian"]})
    )
    assert plan.installable is True
    command = plan.steps[0].command
    assert command[:5] == ["C:/Windows/System32/wsl.exe", "-u", "root", "-d", "Ubuntu"]
    assert "bubblewrap" in command[-1]
    assert plan.requires_elevation is False


def test_darwin_cannot_self_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.platform, "system", lambda: "darwin")
    plan = build_install_plan(_reading(state="not_installed", platform="darwin"))
    assert plan.installable is False
    assert plan.reason == "seatbelt_unavailable"


def test_install_requires_confirmation() -> None:
    result = installer.start_install(confirm=False)
    assert result["ok"] is False
    assert result["detail"] == "install_confirmation_required"


def test_install_rejects_when_nothing_to_do(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "build_install_plan", lambda reading=None: InstallPlan(
        platform="windows",
        install_state="not_installed",
        backend="",
        steps=[],
        notes=[],
        requires_elevation=False,
        may_need_reboot=False,
        uninstall_commands=[],
        installable=False,
        reason="wsl_distro_missing",
    ))
    result = installer.start_install(confirm=True)
    assert result["ok"] is False
    assert result["detail"] == "wsl_distro_missing"


def test_run_install_marks_done_and_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = InstallPlan(
        platform="windows",
        install_state="runtime_missing",
        backend="",
        steps=[InstallStep(id="a", title="A", command=["true"]), InstallStep(id="b", title="B", command=["true"])],
        notes=[],
        requires_elevation=False,
        may_need_reboot=False,
        uninstall_commands=[],
        installable=True,
    )
    monkeypatch.setattr(installer, "_run_step", lambda step: {"id": step.id, "status": "ok", "seconds": 0.1})
    monkeypatch.setattr(installer, "refresh_install_state", lambda: {"state": "ready"})

    installer._STATE.update({"running": True, "steps": [], "error": ""})
    installer._run_install(plan)

    status = installer.install_status()
    assert status["running"] is False
    assert status["phase"] == "done"
    assert [item["id"] for item in status["steps"]] == ["a", "b"]


def test_run_install_stops_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = InstallPlan(
        platform="linux",
        install_state="not_installed",
        backend="",
        steps=[InstallStep(id="a", title="A", command=["x"]), InstallStep(id="b", title="B", command=["y"])],
        notes=[],
        requires_elevation=True,
        may_need_reboot=False,
        uninstall_commands=[],
        installable=True,
    )
    monkeypatch.setattr(
        installer,
        "_run_step",
        lambda step: {"id": step.id, "status": "failed", "output": "boom", "seconds": 0.1},
    )
    monkeypatch.setattr(installer, "refresh_install_state", lambda: {"state": "not_installed"})

    installer._STATE.update({"running": True, "steps": [], "error": ""})
    installer._run_install(plan)

    status = installer.install_status()
    assert status["phase"] == "failed"
    assert "a" in status["error"]
    assert [item["id"] for item in status["steps"]] == ["a"]


def test_uninstall_refuses_without_confirmation() -> None:
    result = installer.start_uninstall(confirm=False)
    assert result["ok"] is False
    assert result["detail"] == "uninstall_confirmation_required"


def test_platform_defaults_to_current_system() -> None:
    plan = build_install_plan(_reading(state="ready"))
    assert plan.platform in {platform_module.system().lower(), "windows", "linux", "darwin"}
