"""全局测试隔离：把持久出站账本钉到用例专属目录。

``external_outbox._workspace_root()`` 走 ``get_runtime_config`` 的 workspace 或
``Path.cwd()``，与 ``MainRuntimeService._workspace_root``（已被 resources/conftest
接管）不是同一条解析路。relay 现在每发一条 ``reply.final`` 都登记账本，任何跑回合
出站的用例都会写到真实仓库的 ``.g3ku/external-outbox/outbox.jsonl`` —— 而那正是
在跑的 web 进程每 60 秒读一次的活账本。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from g3ku.deployment.data_root import DATA_DIR_ENV
from g3ku.deployment.data_root import reset_cache as reset_data_root_cache
from g3ku.runtime import external_outbox


@pytest.fixture(autouse=True)
def _isolate_external_outbox_root(tmp_path: Path):
    external_outbox.configure_external_outbox_root(tmp_path / "external-outbox")
    yield
    external_outbox.configure_external_outbox_root(None)


@pytest.fixture(autouse=True)
def _isolate_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把数据根钉到用例目录：`data_root()` 的第三级回退是 ``Path.cwd()``。

    仓库根跑测试时，凡是走 ``data_root()`` 的存储都会落到正在被 web 进程读取的
    ``.g3ku/``（实测：``tests/test_web_ceo_history_edit_files.py`` 的轮边界快照就写在
    真实 ``.g3ku/web-ceo-turn-boundaries/`` 里，于是用例之间互相看得见对方的文件）。
    ``tests/resources/conftest.py`` 早就这么做，本文件覆盖它上面的整棵树。
    """
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    reset_data_root_cache()
    yield
    reset_data_root_cache()
