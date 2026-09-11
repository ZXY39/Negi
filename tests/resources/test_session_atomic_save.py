"""会话文件全量重写原子化回归测试。

修复缺陷：save() 的全量重写路径直接 open(path, "w") 覆盖，进程崩溃/
断电会留下截断的会话历史。改为同目录临时文件 + fsync + os.replace：
写失败时原会话保持完整，且不残留临时文件。追加快速路径不受影响。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import g3ku.session.manager as manager_module
from g3ku.session.manager import Session, SessionManager


def _read_lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_save_new_session_writes_valid_jsonl_without_temp_residue(tmp_path: Path) -> None:
    manager = SessionManager(workspace=tmp_path)
    session = Session(key="web:demo")
    session.add_message("user", "hello")

    manager.save(session)

    path = manager._get_session_path("web:demo")
    lines = _read_lines(path)
    assert len(lines) == 2, "元数据行 + 一条消息"
    assert json.loads(lines[0])["_type"] == "metadata"
    assert json.loads(lines[1])["content"] == "hello"
    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == [], f"保存不得残留临时文件: {leftovers}"


def test_full_rewrite_failure_keeps_existing_session_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SessionManager(workspace=tmp_path)
    session = Session(key="web:demo")
    session.add_message("user", "hello")
    manager.save(session)
    path = manager._get_session_path("web:demo")
    original_text = path.read_text(encoding="utf-8")

    # 新实例无 _file_states 跟踪 → 必走全量重写路径（standalone 回退同款场景）。
    fresh_manager = SessionManager(workspace=tmp_path)
    crashed_session = Session(key="web:demo")
    crashed_session.add_message("user", "hello")
    crashed_session.add_message("assistant", "world")

    real_dumps = json.dumps
    calls = {"n": 0}

    def _dumps(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:  # 元数据行写成后，第一条消息处模拟崩溃
            raise RuntimeError("simulated crash mid-write")
        return real_dumps(*args, **kwargs)

    monkeypatch.setattr(manager_module.json, "dumps", _dumps)
    with pytest.raises(RuntimeError):
        fresh_manager.save(crashed_session)

    assert path.read_text(encoding="utf-8") == original_text, "写失败必须保留原会话"
    leftovers = [p for p in path.parent.iterdir() if p.name != path.name]
    assert leftovers == [], f"写失败不得残留临时文件: {leftovers}"


def test_append_fast_path_still_works_after_atomic_rewrite(tmp_path: Path) -> None:
    manager = SessionManager(workspace=tmp_path)
    session = Session(key="web:demo")
    session.add_message("user", "first")
    manager.save(session)

    session.add_message("assistant", "second")
    manager.save(session)

    path = manager._get_session_path("web:demo")
    lines = _read_lines(path)
    # 首次全量：元数据 + first；第二次追加：second + 新元数据行。
    assert len(lines) == 4
    contents = [json.loads(line).get("content") for line in lines if "content" in json.loads(line)]
    assert contents == ["first", "second"]
