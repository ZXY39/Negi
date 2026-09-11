"""只读文件强删（fs_utils.remove_tree）回归测试。

修复缺陷：硬删链路此前全部为 shutil.rmtree(ignore_errors=True) 且无只读
文件处理——Windows 上 git 克隆（.git/objects）的只读文件导致整棵目录树
删除静默失败并残留（实例：temp/tasks 残留约 1 GB / 6 万文件）。现在
remove_tree 对失败条目去写位重试，仍有残留时显式告警并返回 False。

跨平台说明：Windows 上文件只读位直接阻止删除（生产事故形态）；POSIX 上
删除文件取决于父目录写权限，只读目录（无 w 位）阻止 unlink 其子项。本
测试同时覆盖两种形态，在两个平台上均有效。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from main.storage import fs_utils
from main.storage.fs_utils import remove_tree


@pytest.fixture()
def writable_tree(tmp_path):
    """测试结束把树恢复成可写权限，避免拖垮 pytest tmp 目录清理。"""
    root = tmp_path / 'tree'
    yield root
    if root.exists():
        for path in [root, *root.rglob('*')]:
            try:
                os.chmod(path, stat.S_IRWXU)
            except OSError:
                pass


def _make_readonly_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('payload', encoding='utf-8')
    os.chmod(path, stat.S_IREAD)
    return path


def test_removes_readonly_files_like_git_clone(writable_tree) -> None:
    root = writable_tree
    _make_readonly_file(root / 'repos' / 'pkg' / '.git' / 'objects' / 'aa' / 'blob1')
    _make_readonly_file(root / 'repos' / 'pkg' / '.git' / 'objects' / 'bb' / 'blob2')
    _make_readonly_file(root / 'normal.txt')

    assert remove_tree(root) is True
    assert not root.exists()


def test_removes_readonly_directory(writable_tree) -> None:
    root = writable_tree
    locked_dir = root / 'locked'
    _make_readonly_file(locked_dir / 'inner.txt')
    os.chmod(locked_dir, stat.S_IREAD | stat.S_IXUSR)  # 目录无写位

    assert remove_tree(root) is True
    assert not root.exists()


def test_missing_path_is_idempotent_success(tmp_path) -> None:
    assert remove_tree(tmp_path / 'never-existed') is True


def test_removes_single_readonly_file(tmp_path) -> None:
    target = tmp_path / 'lonely.bin'
    target.write_bytes(b'x')
    os.chmod(target, stat.S_IREAD)
    try:
        assert remove_tree(target) is True
        assert not target.exists()
    finally:
        if target.exists():
            os.chmod(target, stat.S_IRWXU)


def test_reports_leftover_instead_of_silent_failure(writable_tree, monkeypatch) -> None:
    """删不掉时必须返回 False 并告警——静默残留正是事故根因，禁止回潮。"""
    root = writable_tree
    _make_readonly_file(root / 'f.txt')
    monkeypatch.setattr(fs_utils.shutil, 'rmtree', lambda *a, **kw: (_ for _ in ()).throw(OSError('simulated')))

    assert remove_tree(root) is False
    assert root.exists()


def test_removes_long_path_tree(writable_tree) -> None:
    """Windows MAX_PATH：git 克隆深树的超长路径必须能删（\\\\?\\ 扩展前缀）。"""
    root = writable_tree
    deep = root / 'repos' / 'pkg'
    for i in range(14):  # 每层 ~14 字符，叠加 tmp 基路径后远超 260
        deep = deep / f'nested-dir-{i:02d}'
    deep_file = deep / 'recording.json'
    assert len(str(deep_file)) > 240, '用例前提：路径足够深'

    # 构造超长树本身也受 MAX_PATH 限制：经扩展前缀创建（os 层不做 pathlib 解析）
    create_dir = str(fs_utils._extended_length_path(deep))
    os.makedirs(create_dir, exist_ok=True)
    with open(str(fs_utils._extended_length_path(deep_file)), 'w', encoding='utf-8') as handle:
        handle.write('x' * 64)

    assert remove_tree(root) is True
    assert not root.exists()
