"""「重试次数」的字面语义与写侧闸门回归测试。

覆盖三件事：0 就是 0（不重烧，没有内置默认可退）、负数在每条写车道当场报错而不是被
夹成 0、`null`／没交上来＝不动这一格。轮数在链上怎么被消费由
`test_model_chain_baseline_regression.py` 与 `test_multi_api_key_runtime.py` 钉。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from g3ku.config.loader import load_config
from g3ku.config.model_manager import ModelManager
from g3ku.config.schema import (
    ManagedModelConfig,
    ModelFallbackTarget,
    require_non_negative_retry_count,
)

_MANAGED_BASE = dict(key="x", provider_model="openai:m", api_key="k")


def _write_runtime_config(workspace: Path, *, retry_count: Any = "__omit__") -> None:
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    model_payload: dict[str, Any] = {
        "key": "m",
        "providerModel": "openai:gpt-4.1",
        "apiKey": "demo-key",
        "apiBase": None,
        "extraHeaders": None,
        "enabled": True,
        "maxTokens": 1,
        "temperature": 0.1,
        "reasoningEffort": "low",
        "retryOn": [],
        "description": "",
        "contextWindowTokens": 128000,
    }
    if retry_count != "__omit__":
        model_payload["retryCount"] = retry_count
    payload = {
        "agents": {
            "defaults": {
                "workspace": ".",
                "runtime": "langgraph",
                "maxTokens": 1,
                "temperature": 0.1,
                "maxToolIterations": 1,
                "memoryWindow": 1,
                "reasoningEffort": "low",
            },
            "roleIterations": {"ceo": 40, "execution": 16, "inspection": 16},
            "multiAgent": {"orchestratorModelKey": None},
        },
        "models": {
            "catalog": [model_payload],
            "roles": {"ceo": ["m"], "execution": ["m"], "inspection": ["m"]},
        },
        "providers": {"openai": {"apiKey": "", "apiBase": None, "extraHeaders": None}},
        "web": {"host": "127.0.0.1", "port": 1},
        "toolSecrets": {},
        "resources": {
            "enabled": True,
            "skillsDir": "skills",
            "toolsDir": "tools",
            "manifestName": "resource.yaml",
            "reload": {
                "enabled": True,
                "pollIntervalMs": 1000,
                "debounceMs": 400,
                "lazyReloadOnAccess": True,
                "keepLastGoodVersion": True,
            },
            "locks": {
                "lockDir": ".g3ku/resource-locks",
                "logicalDeleteGuard": True,
                "windowsFsLock": True,
            },
            "statePath": ".g3ku/resources.state.json",
        },
        "mainRuntime": {
            "enabled": True,
            "storePath": ".g3ku/main-runtime/runtime.sqlite3",
            "filesBaseDir": ".g3ku/main-runtime/tasks",
            "artifactDir": ".g3ku/main-runtime/artifacts",
            "governanceStorePath": ".g3ku/main-runtime/governance.sqlite3",
            "defaultMaxDepth": 1,
            "hardMaxDepth": 4,
            "nodeDispatchConcurrency": {"execution": 8, "inspection": 4},
        },
    }
    (workspace / ".g3ku" / "config.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def test_retry_count_zero_is_literal_on_both_schema_models() -> None:
    assert ManagedModelConfig(retry_count=0, **_MANAGED_BASE).retry_count == 0
    assert ManagedModelConfig(**_MANAGED_BASE).retry_count == 0
    assert ModelFallbackTarget(model_key="m", retry_count=0).retry_count == 0


@pytest.mark.parametrize("value", [0, "0", 7, "7"])
def test_require_non_negative_retry_count_accepts_zero_and_positive(value: Any) -> None:
    assert require_non_negative_retry_count(value, field="models.catalog[].retryCount") == int(value)


def test_require_non_negative_retry_count_treats_absent_as_zero() -> None:
    assert require_non_negative_retry_count(None, field="retryCount") == 0
    assert require_non_negative_retry_count("", field="retryCount") == 0
    assert require_non_negative_retry_count("   ", field="retryCount") == 0


def test_require_non_negative_retry_count_rejects_negative() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        require_non_negative_retry_count(-1, field="models.catalog[].retryCount")
    with pytest.raises(ValueError, match="non-negative"):
        require_non_negative_retry_count("-3", field="models.catalog[].retryCount")


def test_schema_retry_count_rejects_negative_with_field_name() -> None:
    # 夹断在这里是禁手：0 现在表示「不重试」，把 -1 悄悄读成 0 等于替操作员关掉重试。
    with pytest.raises(Exception, match="models.catalog\\[\\].retryCount"):
        ManagedModelConfig(retry_count=-1, **_MANAGED_BASE)
    with pytest.raises(Exception, match="models.fallbackTargets\\[\\].retryCount"):
        ModelFallbackTarget(model_key="m", retry_count=-1)


def test_update_model_rejects_negative_without_touching_stored_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`update_model` 是对已有绑定直接赋值，绕开字段上的 `ge=0`——闸门必须在这条路上。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace, retry_count=5)
    monkeypatch.chdir(workspace)

    manager = ModelManager.load()
    with pytest.raises(ValueError, match="non-negative"):
        manager.update_model(key="m", retry_count=-2)

    assert manager.get_model("m")["retry_count"] == 5
    disk = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert disk["models"]["catalog"][0]["retryCount"] == 5


def test_update_model_treats_null_as_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`retry_count: null` 与「没交上来」同义，保持原值（工具侧把 null 读成不动）。"""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace, retry_count=4)
    monkeypatch.chdir(workspace)

    manager = ModelManager.load()
    assert manager.update_model(key="m", retry_count=None)["retry_count"] == 4


def test_update_model_persists_zero_as_zero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace, retry_count=9)
    monkeypatch.chdir(workspace)

    manager = ModelManager.load()
    assert manager.update_model(key="m", retry_count=0)["retry_count"] == 0

    disk = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert disk["models"]["catalog"][0]["retryCount"] == 0
    # 重新读盘也照回 0：没有哪一层把 0 换成内置默认。
    assert load_config().get_managed_model("m").retry_count == 0


def test_facade_update_binding_rejects_negative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace, retry_count=2)
    monkeypatch.chdir(workspace)

    manager = ModelManager.load()
    with pytest.raises(ValueError, match="non-negative"):
        manager.facade.update_binding(
            manager.config,
            model_key="m",
            draft_payload={"retry_count": -1},
        )
    assert manager.get_model("m")["retry_count"] == 2
