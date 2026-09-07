"""Regression tests for the China channel subsystem removal migration.

Locks down the two safety contracts introduced with the deletion:

1. Legacy ``.g3ku/config.json`` files that still carry the retired
   ``chinaBridge`` section must load cleanly (root config forbids extra
   fields) and be re-saved pruned on first load.
2. Orphan ``config.chinaBridge.*`` secret-overlay entries (extracted before
   the removal) must never break config loading: apply re-injects them, the
   migration pops them before validation, and an unlocked save prunes them
   from the overlay permanently.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from g3ku.config.loader import load_config, save_config
from g3ku.security.bootstrap import get_bootstrap_security_service


def _write_config(workspace: Path, *, with_china_bridge: bool) -> None:
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
    }
    if with_china_bridge:
        payload["chinaBridge"] = {
            "enabled": True,
            "autoStart": True,
            "controlToken": "",
            "channels": {"qqbot": {"enabled": False, "accounts": {}}},
        }
    (workspace / ".g3ku" / "config.json").write_text(json.dumps(payload), encoding="utf-8")


def test_legacy_china_bridge_section_is_pruned_on_first_load(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    _write_config(workspace, with_china_bridge=True)
    monkeypatch.chdir(workspace)

    cfg = load_config(workspace / ".g3ku" / "config.json")

    assert not hasattr(cfg, "china_bridge")
    # The load marked the config changed, so it was re-saved pruned.
    saved = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))
    assert "chinaBridge" not in saved
    assert "china_bridge" not in saved
    # Unrelated sections survive the migration untouched.
    assert saved["models"]["roles"]["ceo"] == ["m"]


def test_overlay_orphan_china_bridge_entries_cannot_break_config_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    _write_config(workspace, with_china_bridge=False)
    monkeypatch.chdir(workspace)

    security = get_bootstrap_security_service(workspace)
    security.setup_initial_realm(password="owner-password")
    security.set_overlay_values(
        {
            "config.chinaBridge.controlToken": "orphan-token",
            "config.chinaBridge.channels.qqbot.clientSecret": "orphan-secret",
        }
    )

    # Apply re-injects the orphan entries; the migration must neutralize them
    # before validation instead of failing with "Extra inputs are not
    # permitted".
    cfg = load_config(workspace / ".g3ku" / "config.json")
    assert not hasattr(cfg, "china_bridge")

    # An unlocked save prunes the orphan overlay entries permanently.
    save_config(cfg, workspace / ".g3ku" / "config.json")
    leftovers = [key for key in security.current_overlay() if str(key).startswith("config.chinaBridge")]
    assert leftovers == []
