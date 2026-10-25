"""空闲刷新配置段退役后的加载契约。

根配置是 `extra="forbid"`，所以已安装设备上带 `contextRefresh` 的 config.json
必须在迁移里被剥掉才能继续加载；剥掉后一次保存把它从磁盘上清走。
"""

from __future__ import annotations

import json
from pathlib import Path

from g3ku.config.loader import load_config, save_config
from g3ku.config.schema import Config


def _write_config(workspace: Path) -> Path:
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    path = workspace / ".g3ku" / "config.json"
    save_config(Config(), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["contextRefresh"] = {"enabled": True, "idleMinutes": 45}
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_legacy_idle_refresh_section_still_loads(tmp_path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    path = _write_config(workspace)
    monkeypatch.chdir(workspace)

    cfg = load_config(path)

    assert not hasattr(cfg, "context_refresh")


def test_next_save_prunes_the_retired_section(tmp_path, monkeypatch) -> None:
    """迁移只在加载时剥掉这一节，磁盘上的残留行要等下一次保存才清走。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    path = _write_config(workspace)
    monkeypatch.chdir(workspace)

    save_config(load_config(path), path)

    assert "contextRefresh" not in json.loads(path.read_text(encoding="utf-8"))
