from __future__ import annotations

import json
from pathlib import Path

import pytest

from g3ku.config.model_manager import ModelManager


def _write_runtime_config(workspace: Path, *, include_context_window_tokens: bool = True) -> None:
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
    }
    if include_context_window_tokens:
        model_payload["contextWindowTokens"] = 128000
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
                "chinaBridge": {
                    "enabled": False,
                    "bindHost": "0.0.0.0",
                    "publicPort": 18889,
                    "controlHost": "127.0.0.1",
                    "controlPort": 18989,
                    "controlToken": "",
                    "autoStart": True,
                    "nodeBin": "node",
                    "npmClient": "pnpm",
                    "stateDir": ".g3ku/china-bridge",
                    "logLevel": "info",
                    "sendProgress": True,
                    "sendToolHints": False,
                    "channels": {
                        "qqbot": {"enabled": False, "accounts": {}},
                        "dingtalk": {"enabled": False, "accounts": {}},
                        "wecom": {"enabled": False, "accounts": {}},
                        "wecom-app": {"enabled": False, "accounts": {}},
                        "wecom-kf": {"enabled": False, "accounts": {}},
                        "wechat-mp": {"enabled": False, "accounts": {}},
                        "feishu-china": {"enabled": False, "accounts": {}},
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def test_model_manager_add_model_requires_context_window_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    manager = ModelManager.load()

    with pytest.raises(TypeError):
        manager.add_model(
            key="m2",
            provider_model="openai:gpt-4.1-mini",
            api_key="demo-key",
            api_base="https://api.example.com/v1",
        )


def test_model_manager_rejects_role_chain_save_when_model_lacks_context_window_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace, include_context_window_tokens=False)
    monkeypatch.chdir(workspace)

    manager = ModelManager.load()

    with pytest.raises(ValueError):
        manager.update_scope_routes_bulk({"ceo": {"model_keys": ["m"]}})
