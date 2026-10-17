"""密钥落盘必须原子：写不进就保留上一份，不许留下 0 字节钥匙。

修复缺陷：master.key 信封（_json_dump）与 auto-unlock 钥匙都用
write_text 直写目标文件——write_text 先截断再写，一次 ENOSPC 就把钥匙
清零，之后每次解锁与每次保存都被解密守卫反拒（同类事故已在 overlay
上发生过，见 _atomic_write_bytes 的 docstring）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import g3ku.security.bootstrap as bootstrap_module
from g3ku.security.bootstrap import BootstrapSecurityService


def _boom_fsync(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(bootstrap_module.os, "fsync", _fail)


def test_json_dump_failure_keeps_previous_key_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / ".g3ku" / "master.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    original_text = '{"version": 1, "realm": "original"}\n'
    path.write_text(original_text, encoding="utf-8")

    _boom_fsync(monkeypatch)
    with pytest.raises(OSError):
        bootstrap_module._json_dump(path, {"version": 1, "realm": "replacement"})

    assert path.read_text(encoding="utf-8") == original_text, "写失败必须保留原钥匙文件"
    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == [], f"不得残留临时文件: {leftovers}"


def test_auto_unlock_failure_keeps_previous_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = BootstrapSecurityService(tmp_path)
    service._active_master_key = "new-key-material"
    path = bootstrap_module._auto_unlock_key_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("old-key-material\n", encoding="utf-8")
    monkeypatch.setenv(bootstrap_module.BOOTSTRAP_MASTER_KEY_ENV, "")

    _boom_fsync(monkeypatch)
    with pytest.raises(OSError):
        service.set_auto_unlock(enabled=True)

    assert path.read_text(encoding="utf-8") == "old-key-material\n", "写失败必须保留旧钥匙"
    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == [], f"不得残留临时文件: {leftovers}"


def test_auto_unlock_success_writes_the_active_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = BootstrapSecurityService(tmp_path)
    service._active_master_key = "fresh-key-material"
    monkeypatch.setattr(bootstrap_module, "snapshot_key_store", lambda workspace, *, reason: None)
    monkeypatch.setenv(bootstrap_module.BOOTSTRAP_MASTER_KEY_ENV, "")

    service.set_auto_unlock(enabled=True)

    path = bootstrap_module._auto_unlock_key_path(tmp_path)
    assert path.read_text(encoding="utf-8") == "fresh-key-material\n"
    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == [], f"成功写入不得残留临时文件: {leftovers}"
