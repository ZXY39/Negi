"""一次性恢复脚本（packaging/recovery.py）的解析与准入逻辑。

只测纯逻辑：安装根/端口/版本解析、uv 探测、非安装根拒入。不触发真实退出/下载/换码。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "packaging" / "recovery.py"


def _load():
    spec = importlib.util.spec_from_file_location("negi_recovery", RECOVERY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolve_root_reads_the_install_pointer(tmp_path: Path, monkeypatch):
    rec = _load()
    local = tmp_path / "local"
    (local / "Negi").mkdir(parents=True)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    (local / "Negi" / "install-root.json").write_text(json.dumps({"root": str(chosen)}), encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    assert rec._resolve_root(None) == chosen.resolve()
    assert rec._resolve_root(str(tmp_path)) == tmp_path.resolve()


def test_resolve_port_prefers_config_over_default(tmp_path: Path):
    rec = _load()
    (tmp_path / ".g3ku").mkdir()
    (tmp_path / ".g3ku" / "config.json").write_text(json.dumps({"web": {"port": 18999}}), encoding="utf-8")
    assert rec._resolve_port(tmp_path, None) == 18999
    assert rec._resolve_port(tmp_path, 19000) == 19000
    assert rec._resolve_port(tmp_path / "missing", None) == 18790


def test_resolve_ref_picks_the_highest_semver_tag(tmp_path: Path, monkeypatch):
    rec = _load()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(rec.shutil, "which", lambda name: "git")

    class _Done:
        stdout = (
            "aaa\trefs/tags/v1.9.0\n"
            "bbb\trefs/tags/v1.11.23\n"
            "ccc\trefs/tags/v1.11.23^{}\n"
            "ddd\trefs/tags/v1.11.9\n"
            "eee\trefs/tags/backup/old\n"
        )

    monkeypatch.setattr(rec.subprocess, "run", lambda *a, **k: _Done())
    assert rec._resolve_ref(tmp_path, None) == "v1.11.23"
    assert rec._resolve_ref(tmp_path, "v2.0.0") == "v2.0.0"


def test_resolve_ref_without_git_or_remote_asks_for_explicit(tmp_path: Path, monkeypatch):
    rec = _load()
    monkeypatch.setattr(rec.shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        rec._resolve_ref(tmp_path, None)


def test_local_uv_probes_the_persisted_locations(tmp_path: Path, monkeypatch):
    rec = _load()
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(rec.shutil, "which", lambda name: None)
    assert rec._local_uv() is None
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "uv.exe").write_bytes(b"stub")
    assert rec._local_uv() == str(bin_dir / "uv.exe")


def test_main_refuses_a_directory_that_is_not_an_install(tmp_path: Path):
    rec = _load()
    with pytest.raises(SystemExit):
        rec.main(["--root", str(tmp_path), "--ref", "v1.0.0"])


def test_recovery_cmd_finds_an_interpreter_without_delayed_expansion():
    text = (REPO_ROOT / "packaging" / "recovery.cmd").read_text(encoding="utf-8")
    # 用 goto 标签而非括号块，避免 %ERRORLEVEL% 在块解析时提前展开。
    assert ":use_py" in text and ":use_venv" in text
    assert "recovery.py" in text
    assert "if not errorlevel 1 goto use_py" in text
