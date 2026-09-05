from __future__ import annotations

from pathlib import Path

import pytest

from main.service.runtime_service import MainRuntimeService

# 在 pytest 生命周期内，任何未显式指定工作区的运行时服务，其 `_workspace_root()` 的
# cwd 回退都会被替换为当前用例专属的临时目录，避免任务临时目录（temp/tasks）
# 泄漏到真实仓库工作区（曾在真实仓库里累积出 26000+ 个孤儿 task_* 文件夹）。
_ORIGINAL_WORKSPACE_ROOT = MainRuntimeService._workspace_root


@pytest.fixture(autouse=True)
def _isolate_runtime_workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _isolated_workspace_root(self) -> Path:
        original = _ORIGINAL_WORKSPACE_ROOT(self)
        override = getattr(self, '_workspace_root_override', None)
        manager = getattr(self, '_resource_manager', None)
        manager_workspace = getattr(manager, 'workspace', None)
        if override is None and manager_workspace is None:
            # 只有原始实现会回退到 Path.cwd() 的分支才替换为 tmp_path。
            return Path(tmp_path).resolve(strict=False)
        return original

    monkeypatch.setattr(MainRuntimeService, '_workspace_root', _isolated_workspace_root)