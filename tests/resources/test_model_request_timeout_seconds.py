"""per-model request_timeout_seconds（请求超时时间）回归测试。

覆盖：schema 校验、model_manager 持久化/回显、chat backend 按模型解析与优先级
（调用方显式 → 模型配置 → 全局默认 600）。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.config.model_manager import ModelManager
from g3ku.config.schema import ManagedModelConfig
from g3ku.llm_config.enums import ProbeStatus


def _write_runtime_config(workspace: Path, *, request_timeout_seconds: float | None = None) -> None:
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    model_payload: dict[str, object] = {
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
    if request_timeout_seconds is not None:
        model_payload["requestTimeoutSeconds"] = request_timeout_seconds
    (workspace / ".g3ku" / "config.json").write_text(
        json.dumps(
            {
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
        ),
        encoding="utf-8",
    )


def test_schema_request_timeout_seconds_accepts_positive_and_blank() -> None:
    base = dict(key="x", provider_model="openai:m", api_key="k")
    assert ManagedModelConfig(request_timeout_seconds="30", **base).request_timeout_seconds == 30.0
    assert ManagedModelConfig(request_timeout_seconds=90.5, **base).request_timeout_seconds == 90.5
    assert ManagedModelConfig(request_timeout_seconds="", **base).request_timeout_seconds is None
    assert ManagedModelConfig(**base).request_timeout_seconds is None


def test_schema_request_timeout_seconds_rejects_non_positive() -> None:
    base = dict(key="x", provider_model="openai:m", api_key="k")
    for value in (0, -1, "-3"):
        with pytest.raises(Exception):
            ManagedModelConfig(request_timeout_seconds=value, **base)


def test_model_manager_update_model_persists_request_timeout_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    manager = ModelManager.load()
    monkeypatch.setattr(
        manager.facade.config_service,
        "probe_draft",
        lambda draft: SimpleNamespace(success=True, status=ProbeStatus.SUCCESS, message="ok"),
    )

    updated = manager.update_model(key="m", request_timeout_seconds=120)

    assert updated["request_timeout_seconds"] == 120.0
    assert ModelManager.load().get_model("m")["request_timeout_seconds"] == 120.0


def test_model_manager_update_model_clears_request_timeout_seconds_with_blank(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace, request_timeout_seconds=90)
    monkeypatch.chdir(workspace)

    manager = ModelManager.load()
    monkeypatch.setattr(
        manager.facade.config_service,
        "probe_draft",
        lambda draft: SimpleNamespace(success=True, status=ProbeStatus.SUCCESS, message="ok"),
    )

    assert manager.get_model("m")["request_timeout_seconds"] == 90.0
    updated = manager.update_model(key="m", request_timeout_seconds=None)
    assert updated["request_timeout_seconds"] is None


def test_catalog_request_timeout_survives_config_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace, request_timeout_seconds=45)
    monkeypatch.chdir(workspace)

    manager = ModelManager.load()
    assert manager.config.get_managed_model("m").request_timeout_seconds == 45.0
    assert manager.get_model("m")["request_timeout_seconds"] == 45.0


def test_chat_backend_recommended_timeout_resolves_per_model_from_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace, request_timeout_seconds=75)
    monkeypatch.chdir(workspace)

    from g3ku.config.loader import load_config
    from main.runtime.chat_backend import (
        DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS,
        ConfigChatBackend,
    )

    backend = ConfigChatBackend(config=load_config())
    # 链上第一个配置了超时的模型生效。
    assert backend.recommended_model_response_timeout_seconds(model_refs=["m"]) == 75.0
    # 未配置的模型回退全局默认 600。
    assert (
        backend.recommended_model_response_timeout_seconds(model_refs=["unknown-model"])
        == DEFAULT_PROVIDER_ATTEMPT_TIMEOUT_SECONDS
    )
    # 直接解析器同语义。
    assert backend.model_configured_request_timeout_seconds("m") == 75.0
    assert backend.model_configured_request_timeout_seconds("unknown-model") is None
