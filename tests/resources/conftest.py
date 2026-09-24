from __future__ import annotations

from pathlib import Path

import pytest

from g3ku.deployment.data_root import DATA_DIR_ENV
from g3ku.deployment.data_root import reset_cache as reset_data_root_cache
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

    # 数据根同样钉到本用例 tmp_path：默认安装里数据根与工作区根重合，测试保持这一
    # 不变量，否则换锚后的存储会写进真实仓库。
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    reset_data_root_cache()
    monkeypatch.setattr(MainRuntimeService, '_workspace_root', _isolated_workspace_root)
    yield
    reset_data_root_cache()
