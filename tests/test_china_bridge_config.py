from __future__ import annotations

import json

import pytest

from g3ku.config.loader import _migrate_config, load_config


def test_migrate_config_normalizes_china_bridge_channel_aliases():
    raw = {
        "chinaBridge": {
            "enabled": True,
            "publicPort": 19999,
            "channels": {
                "wecom-app": {"enabled": True, "token": "demo"},
                "feishu-china": {"enabled": True, "appId": "cli_demo"},
            },
        },
    }

    migrated = _migrate_config(raw)

    assert migrated["china_bridge"]["enabled"] is True
    assert migrated["china_bridge"]["channels"]["wecom-app"]["token"] == "demo"
    assert migrated["china_bridge"]["channels"]["feishu-china"]["appId"] == "cli_demo"


def test_load_config_rejects_legacy_channels_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
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
                    "multiAgent": {"orchestratorModelKey": None},
                },
                "channels": {
                    "sendProgress": True,
                    "qq": {"enabled": True, "appId": "123"},
                },
                "models": {
                    "catalog": [
                        {
                            "key": "m",
                            "providerModel": "openai:gpt-4.1",
                            "apiKey": "demo-key",
                            "enabled": True,
                            "maxTokens": 1,
                            "temperature": 0.1,
                            "retryOn": [],
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
                    "reload": {"enabled": True, "pollIntervalMs": 1000, "debounceMs": 400, "lazyReloadOnAccess": True, "keepLastGoodVersion": True},
                    "locks": {"lockDir": ".g3ku/resource-locks", "logicalDeleteGuard": True, "windowsFsLock": True},
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
                },
                "chinaBridge": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)

    with pytest.raises(ValueError, match="Legacy channels config has been removed"):
        load_config()


def test_load_config_migrates_legacy_gateway_bind_config(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
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
                    "multiAgent": {"orchestratorModelKey": None},
                },
                "models": {
                    "catalog": [
                        {
                            "key": "m",
                            "providerModel": "openai:gpt-4.1",
                            "apiKey": "demo-key",
                            "enabled": True,
                            "maxTokens": 1,
                            "temperature": 0.1,
                            "retryOn": [],
                            "description": "",
                        }
                    ],
                    "roles": {"ceo": ["m"], "execution": ["m"], "inspection": ["m"]},
                },
                "providers": {"openai": {"apiKey": "", "apiBase": None, "extraHeaders": None}},
                "gateway": {"host": "0.0.0.0", "port": 18790, "heartbeat": {"enabled": True, "intervalS": 60}},
                "toolSecrets": {},
                "resources": {
                    "enabled": True,
                    "skillsDir": "skills",
                    "toolsDir": "tools",
                    "manifestName": "resource.yaml",
                    "reload": {"enabled": True, "pollIntervalMs": 1000, "debounceMs": 400, "lazyReloadOnAccess": True, "keepLastGoodVersion": True},
                    "locks": {"lockDir": ".g3ku/resource-locks", "logicalDeleteGuard": True, "windowsFsLock": True},
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
                },
                "chinaBridge": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace)

    cfg = load_config()

    assert cfg.web.host == "0.0.0.0"
    assert cfg.web.port == 18790
    saved = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert saved["web"] == {"host": "0.0.0.0", "port": 18790}
    assert "gateway" not in saved
