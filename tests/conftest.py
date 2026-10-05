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

from g3ku.runtime import external_outbox


@pytest.fixture(autouse=True)
def _isolate_external_outbox_root(tmp_path: Path):
    external_outbox.configure_external_outbox_root(tmp_path / "external-outbox")
    yield
    external_outbox.configure_external_outbox_root(None)
