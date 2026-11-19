import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from g3ku.deployment import autostart  # noqa: E402


class FakeKey:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeWinReg:
    HKEY_CURRENT_USER = 1
    KEY_READ = 2
    KEY_SET_VALUE = 4
    REG_SZ = 1

    def __init__(self, value=None):
        self.value = value
        self.written = None

    def OpenKey(self, *args, **kwargs):  # noqa: N802 - winreg 的方法名
        return FakeKey()

    def QueryValueEx(self, key, name):  # noqa: N802
        if self.value is None:
            raise FileNotFoundError()
        return self.value, self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, data):  # noqa: N802
        self.written = data

    def DeleteValue(self, key, name):  # noqa: N802
        self.value = None


def _nt_root(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.os, "name", "nt")
    system_root = tmp_path / "Windows"
    (system_root / "System32").mkdir(parents=True)
    (system_root / "System32" / "wscript.exe").write_text("", encoding="utf-8")
    monkeypatch.setenv("SystemRoot", str(system_root))
    (tmp_path / "negi-start.vbs").write_text("", encoding="utf-8")
    return tmp_path


def test_launcher_command_windows_uses_hidden_vbs(tmp_path, monkeypatch):
    root = _nt_root(tmp_path, monkeypatch)
    argv = autostart.launcher_command(root)
    assert argv == [
        str(root / "Windows" / "System32" / "wscript.exe"),
        str(root / "negi-start.vbs"),
        "start",
        "--no-browser",
    ]


def test_launcher_command_windows_falls_back_to_cmd(tmp_path, monkeypatch):
    root = _nt_root(tmp_path, monkeypatch)
    (root / "Windows" / "System32" / "wscript.exe").unlink()
    argv = autostart.launcher_command(root)
    assert argv == [str(root / "negi.cmd"), "start", "--no-browser"]


def test_launcher_command_posix_uses_sh(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.os, "name", "posix")
    argv = autostart.launcher_command(tmp_path)
    assert argv == ["/bin/sh", str(tmp_path / "negi.sh"), "start", "--no-browser"]


def test_legacy_launcher_command_is_venv_hosted(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.os, "name", "nt")
    argv = autostart.legacy_launcher_command(tmp_path)
    assert argv == [
        str(tmp_path / ".venv" / "Scripts" / "pythonw.exe"),
        str(tmp_path / "negi_bootstrap.py"),
        "start",
        "--no-browser",
    ]


def test_registered_argv_windows_parses_quoted_string(monkeypatch):
    monkeypatch.setitem(autostart._BACKEND_CACHE, "backend", "windows")
    legacy = autostart.legacy_launcher_command()
    raw = " ".join(f'"{item}"' for item in legacy)
    fake = FakeWinReg(raw)
    argv = autostart.registered_argv(winreg_module=fake)
    assert argv == legacy


def test_registered_argv_windows_empty_means_none(monkeypatch):
    monkeypatch.setitem(autostart._BACKEND_CACHE, "backend", "windows")
    assert autostart.registered_argv(winreg_module=FakeWinReg(None)) is None


def test_registered_argv_systemd_parses_execstart(tmp_path, monkeypatch):
    monkeypatch.setattr(autostart.os, "name", "posix")
    monkeypatch.setattr(autostart, "_BACKEND_CACHE", {"backend": "systemd-user"})
    monkeypatch.setattr(autostart.Path, "home", lambda: tmp_path)
    unit = tmp_path / ".config" / "systemd" / "user" / "negi.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(
        '[Service]\nExecStart="/opt/Negi/.venv/bin/python" "/opt/Negi/negi_bootstrap.py" start --no-browser\n',
        encoding="utf-8",
    )
    argv = autostart.registered_argv()
    assert argv == ["/opt/Negi/.venv/bin/python", "/opt/Negi/negi_bootstrap.py", "start", "--no-browser"]
