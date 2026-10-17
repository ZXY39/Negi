"""打不开的子树要留下可数落点：指纹不抛，但缺的那部分必须点名。

`_collect_tree_entries` 此前 `except OSError: return` 一声不响——权限/长路径失败时那棵子树
永久不进指纹，改了正文也判成没变。判据不能反过来变成哑点，所以这里钉两件事：指纹照出、
失败点名一次、恢复时也收口一次。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from loguru import logger

from g3ku.resources.registry import ResourceRegistry


@pytest.fixture()
def sink() -> list[str]:
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level="INFO")
    try:
        yield messages
    finally:
        logger.remove(sink_id)
    ResourceRegistry._FINGERPRINT_FAILURES.clear()


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    nested = root / "demo_skill"
    nested.mkdir(parents=True)
    (root / "SKILL.md").write_text("top", encoding="utf-8")
    (nested / "SKILL.md").write_text("inner", encoding="utf-8")
    return root


def test_unreadable_subtree_is_reported_once_and_fingerprint_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sink: list[str]
) -> None:
    root = _tree(tmp_path)
    blocked = str(root / "demo_skill")
    real_scandir = os.scandir

    def _fake_scandir(path, *args, **kwargs):
        if str(path) == blocked:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", _fake_scandir)

    digest = ResourceRegistry._tree_fingerprint(root)

    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    warnings = [text for text in sink if "unreadable" in text]
    assert len(warnings) == 1, warnings
    assert blocked in warnings[0]

    # 失败期间那棵子树的正文改动判不出来——这正是告警替维护者点名的后果
    (root / "demo_skill" / "SKILL.md").write_text("changed-while-blocked", encoding="utf-8")
    assert ResourceRegistry._tree_fingerprint(root) == digest

    # 同一失败集不逐条命令刷屏
    ResourceRegistry._tree_fingerprint(root)
    assert len([text for text in sink if "unreadable" in text]) == 1


def test_recovery_is_announced_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sink: list[str]
) -> None:
    root = _tree(tmp_path)
    blocked = str(root / "demo_skill")
    real_scandir = os.scandir
    state = {"block": True}

    def _fake_scandir(path, *args, **kwargs):
        if state["block"] and str(path) == blocked:
            raise OSError(36, "File name too long", str(path))
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", _fake_scandir)

    ResourceRegistry._tree_fingerprint(root)
    state["block"] = False
    ResourceRegistry._tree_fingerprint(root)
    ResourceRegistry._tree_fingerprint(root)

    assert any("unreadable" in text for text in sink)
    recovered = [text for text in sink if "readable again" in text]
    assert len(recovered) == 1, sink
    assert ResourceRegistry._FINGERPRINT_FAILURES == {}
