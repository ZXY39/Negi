"""Read-only CLI diagnostics for the External Agent API surface.

``g3ku external status`` / ``g3ku external sessions`` give shell users (and
the agent's exec tool) a way to inspect bridge connectivity state without
touching secrets: tokens render masked (or as a locked-placeholder note) and
the registry mapping is listed verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner


def _write_config(workspace: Path) -> None:
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
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
            "catalog": [
                {
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
            "nodeDispatchConcurrency": {"execution": 8, "inspection": 4},
        },
        "externalApi": {
            "enabled": True,
            "eventBufferSize": 512,
            "tokens": {"napcat": {"token": "", "label": "家里 NapCat", "enabled": True}},
        },
    }
    (workspace / ".g3ku" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def runner_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    workspace = tmp_path / "workspace"
    _write_config(workspace)
    monkeypatch.chdir(workspace)
    monkeypatch.setattr("g3ku.runtime.external_sessions._registry_workspace", lambda: workspace)
    return workspace


def test_external_status_lists_masked_tokens(runner_workspace: Path) -> None:
    from g3ku.cli.commands import app

    result = CliRunner().invoke(app, ["external", "status"])
    assert result.exit_code == 0, result.output
    assert "enabled" in result.output
    assert "napcat" in result.output
    assert "家里 NapCat" in result.output


def test_external_sessions_lists_registry_entries(runner_workspace: Path) -> None:
    from g3ku.cli.commands import app
    from g3ku.runtime.external_sessions import ExternalSessionRegistry

    registry = ExternalSessionRegistry(runner_workspace)
    entry, _ = registry.resolve_or_create(bridge_id="napcat", external_key="qq:dm:user-9")

    result = CliRunner().invoke(app, ["external", "sessions"], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    assert "qq:dm:user-9" in result.output
    assert entry.session_key in result.output

    filtered = CliRunner().invoke(app, ["external", "sessions", "--bridge", "other"], env={"COLUMNS": "300"})
    assert filtered.exit_code == 0, filtered.output
    assert "No external bridge sessions registered." in filtered.output
