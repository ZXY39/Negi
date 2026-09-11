"""config.json 原子写回归测试。

修复缺陷：save_config 直接 open(path, "w") 覆盖写，进程崩溃/断电会留下
截断的半 JSON 配置。现改为同目录临时文件 + fsync + os.replace：写失败时
原配置保持完整，且不得残留临时文件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import g3ku.config.loader as loader_module
from g3ku.config.loader import save_config
from g3ku.config.schema import Config


def test_save_failure_keeps_previous_config_and_leaves_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".g3ku" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    original_text = '{"sentinel": "original"}\n'
    path.write_text(original_text, encoding="utf-8")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash mid-write")

    monkeypatch.setattr(loader_module.json, "dump", _boom)
    with pytest.raises(RuntimeError):
        save_config(Config(), path)

    assert path.read_text(encoding="utf-8") == original_text, "写失败必须保留原配置"
    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == [], f"不得残留临时文件: {leftovers}"


def test_successful_save_is_valid_json_without_temp_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".g3ku" / "config.json"

    save_config(Config(), path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == [], f"成功保存不得残留临时文件: {leftovers}"
