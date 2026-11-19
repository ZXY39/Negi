import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from g3ku.deployment import autostart, entry_repair  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_shortcut_lane(monkeypatch):
    # repair_entries 的快捷方式车道会真跑 PowerShell COM 改本机桌面——单元测试
    # 必须把它隔离掉，只测判定逻辑。subprocess.run 全局替换、monkeypatch 还原。
    class Done:
        stdout = ""
        returncode = 0

    monkeypatch.setattr(entry_repair.subprocess, "run", lambda *a, **k: Done())


def test_migrates_exact_legacy_shape(monkeypatch):
    legacy = autostart.legacy_launcher_command()
    monkeypatch.setattr(autostart, "registered_argv", lambda **kw: legacy)
    applied = []
    monkeypatch.setattr(autostart, "apply_enabled", lambda enabled, **kw: applied.append(enabled) or {"ok": True})
    report = entry_repair.repair_entries()
    assert report["autostart"] == "migrated"
    assert applied == [True]


def test_legacy_match_is_case_insensitive_on_windows(monkeypatch):
    monkeypatch.setattr(entry_repair.os, "name", "nt")
    legacy = [item.upper() for item in autostart.legacy_launcher_command()]
    monkeypatch.setattr(autostart, "registered_argv", lambda **kw: legacy)
    monkeypatch.setattr(autostart, "apply_enabled", lambda enabled, **kw: {"ok": True})
    assert entry_repair.repair_entries()["autostart"] == "migrated"


def test_custom_shape_untouched(monkeypatch):
    custom = [r"C:\Python313\python.exe", r"D:\Negi\negi_bootstrap.py", "web"]
    monkeypatch.setattr(autostart, "registered_argv", lambda **kw: custom)
    applied = []
    monkeypatch.setattr(autostart, "apply_enabled", lambda enabled, **kw: applied.append(enabled) or {"ok": True})
    report = entry_repair.repair_entries()
    assert report["autostart"] == "untouched"
    assert applied == []


def test_absent_registration_untouched(monkeypatch):
    monkeypatch.setattr(autostart, "registered_argv", lambda **kw: None)
    assert entry_repair.repair_entries()["autostart"] == "untouched"


def test_repair_never_raises(monkeypatch):
    def boom(**kw):
        raise OSError("registry policy")

    monkeypatch.setattr(autostart, "registered_argv", boom)
    report = entry_repair.repair_entries()
    assert str(report["autostart"]).startswith("failed:")


def test_shortcut_script_targets_only_venv_hosts(tmp_path):
    script = entry_repair._repair_shortcut_script(tmp_path)
    assert "Negi.lnk" in script
    assert ".venv" in script
    assert "negi_bootstrap.py" in script
    assert "negi-start.vbs" in script
    assert str(tmp_path).replace("'", "''") in script
