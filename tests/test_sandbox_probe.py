"""Windows 后端探测：用现有发行版还是专用发行版。

契约见 ``docs/architecture/sandbox-execution-plane.md``「档位存储与 API」的安装态一条。
这些用例把 `wsl.exe` 的输出全部替身化，跑在任意平台都能验。
"""

from __future__ import annotations

import pytest

import main  # noqa: F401  (production import order)
from g3ku.sandbox import probe


def _with_wsl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probe.shutil, "which", lambda name: "C:/Windows/System32/wsl.exe" if name in {"wsl", "wsl.exe"} else None
    )


def _fake_run(listing: str, capabilities: dict[str, str]):
    calls: list[list[str]] = []

    def runner(argv, *, encoding="utf-8"):  # noqa: ANN001
        calls.append(list(argv))
        if "-l" in argv:
            return 0, listing
        distro = argv[argv.index("-d") + 1]
        return 0, capabilities.get(distro, "||")

    return runner, calls


def test_without_wsl_or_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe.shutil, "which", lambda name: None)
    reading = probe._probe_windows()
    assert reading["state"] == "not_installed"
    assert reading["reason"] == "wsl_distro_missing"


def test_wsl_without_any_distro(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_wsl(monkeypatch)
    runner, _calls = _fake_run("", {})
    monkeypatch.setattr(probe, "_run", runner)
    reading = probe._probe_windows()
    assert reading["state"] == "runtime_missing"
    assert reading["reason"] == "wsl_distro_missing"
    assert reading["detail"]["distros"] == []


def test_existing_distro_qualifies(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_wsl(monkeypatch)
    runner, _calls = _fake_run(
        "Ubuntu\r\n",
        {"Ubuntu": "/usr/bin/bwrap|/usr/bin/python3|1"},
    )
    monkeypatch.setattr(probe, "_run", runner)
    reading = probe._probe_windows()
    assert reading["state"] == "ready"
    assert reading["backend"] == probe.BACKEND_WSL_BWRAP
    assert reading["detail"]["selected_distro"] == "Ubuntu"
    assert reading["detail"]["dedicated"] is False
    assert reading["detail"]["wsl_interop"] is True
    assert reading["enforcement"]["fs_read"] == probe.FULL
    assert reading["enforcement"]["network"] == probe.FULL


def test_dedicated_distro_is_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_wsl(monkeypatch)
    runner, calls = _fake_run(
        "Ubuntu\r\nnegi-sandbox\r\n",
        {
            "Ubuntu": "/usr/bin/bwrap|/usr/bin/python3|1",
            "negi-sandbox": "/usr/bin/bwrap|/usr/bin/python3|0",
        },
    )
    monkeypatch.setattr(probe, "_run", runner)
    reading = probe._probe_windows()
    assert reading["state"] == "ready"
    assert reading["detail"]["selected_distro"] == "negi-sandbox"
    assert reading["detail"]["dedicated"] is True
    # 专用发行版排在最前，命中即停：不去打扰用户自己的发行版
    assert calls[1][:3] == ["C:/Windows/System32/wsl.exe", "-d", "negi-sandbox"]


def test_distro_without_bwrap_reports_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_wsl(monkeypatch)
    runner, _calls = _fake_run("Ubuntu\r\n", {"Ubuntu": "|/usr/bin/python3|1"})
    monkeypatch.setattr(probe, "_run", runner)
    reading = probe._probe_windows()
    assert reading["state"] == "runtime_missing"
    assert reading["reason"] == "bwrap_missing_in_distro"
    assert "bubblewrap" in reading["detail"]["install_hint"]


def test_distro_without_python_is_not_qualified(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_wsl(monkeypatch)
    runner, _calls = _fake_run("Alpine\r\n", {"Alpine": "/usr/bin/bwrap||0"})
    monkeypatch.setattr(probe, "_run", runner)
    reading = probe._probe_windows()
    assert reading["state"] == "runtime_missing"
    assert reading["reason"] == "no_qualified_distro"
    assert reading["detail"]["checked"][0]["qualified"] is False


def test_distro_probe_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_wsl(monkeypatch)
    many = "\r\n".join(f"distro-{index}" for index in range(8))
    runner, calls = _fake_run(many, {})
    monkeypatch.setattr(probe, "_run", runner)
    probe._probe_windows()
    # 一次列发行版 + 至多 N 次逐发行版探测
    assert len(calls) == 1 + probe._WSL_MAX_DISTRO_CHECKS
