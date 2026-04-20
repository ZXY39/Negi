from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from g3ku.config.model_manager import ModelManager
from g3ku.llm_config.enums import ProbeStatus


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


def test_model_manager_update_model_persists_context_window_tokens(
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

    updated = manager.update_model(key="m", context_window_tokens=196000)

    assert updated["context_window_tokens"] == 196000
    assert ModelManager.load().get_model("m")["context_window_tokens"] == 196000
    saved = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert saved["models"]["catalog"][0]["contextWindowTokens"] == 196000


def test_model_manager_defaults_image_multimodal_enabled_to_false_for_legacy_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_runtime_config(workspace)
    monkeypatch.chdir(workspace)

    manager = ModelManager.load()

    assert manager.config.get_managed_model("m").image_multimodal_enabled is False
    assert manager.get_model("m")["image_multimodal_enabled"] is False


def test_model_manager_update_model_persists_image_multimodal_enabled_without_touching_provider_record(
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

    updated = manager.update_model(key="m", image_multimodal_enabled=True)

    assert updated["image_multimodal_enabled"] is True
    assert ModelManager.load().get_model("m")["image_multimodal_enabled"] is True

    saved = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert saved["models"]["catalog"][0]["imageMultimodalEnabled"] is True

    record_dir = workspace / ".g3ku" / "llm-config" / "records"
    record_files = list(record_dir.glob("*.json"))
    assert record_files
    record_payload = json.loads(record_files[0].read_text(encoding="utf-8"))
    assert "image_multimodal_enabled" not in record_payload
    assert "imageMultimodalEnabled" not in record_payload


def test_model_manager_backfills_context_window_tokens_from_llm_config_record_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku" / "llm-config" / "records").mkdir(parents=True, exist_ok=True)

    config_id = "cfg-1"
    (workspace / ".g3ku" / "llm-config" / "records" / f"{config_id}.json").write_text(
        json.dumps(
            {
                "config_id": config_id,
                "provider_id": "openai",
                "display_name": "OpenAI",
                "protocol_adapter": "openai-responses",
                "capability": "chat",
                "auth_mode": "api_key",
                "base_url": "https://api.example.com/v1",
                "default_model": "gpt-4.1",
                "auth": {"type": "api_key", "api_key": "demo-key"},
                "parameters": {"context_window_tokens": 128000},
                "headers": {},
                "extra_options": {},
                "template_version": "test",
                "created_at": "2026-04-19T00:00:00Z",
                "updated_at": "2026-04-19T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    # Note: No `contextWindowTokens` in `models.catalog`. This simulates older installs where
    # the bound `llm-config` record has the correct window, but `.g3ku/config.json` didn't
    # persist it yet.
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
                    "catalog": [
                        {
                            "key": "m",
                            "llmConfigId": config_id,
                            "enabled": True,
                            "retryOn": [],
                            "retryCount": 0,
                            "description": "",
                        }
                    ],
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

    monkeypatch.chdir(workspace)

    manager = ModelManager.load()
    assert manager.config.get_managed_model("m").context_window_tokens is None

    manager.update_scope_routes_bulk({"ceo": {"model_keys": ["m"]}})

    reloaded = ModelManager.load()
    assert reloaded.config.get_managed_model("m").context_window_tokens == 128000
    saved = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert saved["models"]["catalog"][0]["contextWindowTokens"] == 128000
