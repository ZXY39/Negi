from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import g3ku.china_bridge.supervisor as supervisor_module
from g3ku.china_bridge.supervisor import ChinaBridgeSupervisor
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


class _BridgeTestTransport:
    def __init__(self) -> None:
        self.sender = None

    async def handle_frame(self, _payload: dict) -> None:
        return None

    def set_sender(self, sender) -> None:
        self.sender = sender


def _build_supervisor(workspace: Path) -> ChinaBridgeSupervisor:
    host_root = workspace / "subsystems" / "china_channels_host"
    (host_root / "src").mkdir(parents=True, exist_ok=True)
    (host_root / "dist").mkdir(parents=True, exist_ok=True)
    (host_root / "package.json").write_text("{}", encoding="utf-8")
    (host_root / "tsconfig.json").write_text("{}", encoding="utf-8")
    (host_root / "upstream_map.yaml").write_text("channels: []\n", encoding="utf-8")
    (host_root / "dist" / "index.js").write_text("export {};\n", encoding="utf-8")
    config = SimpleNamespace(
        china_bridge=SimpleNamespace(
            enabled=True,
            auto_start=True,
            public_port=18889,
            control_port=18989,
            control_host="127.0.0.1",
            control_token="",
            node_bin="node",
            npm_client="pnpm",
            state_dir=".g3ku/china-bridge",
        )
    )
    return ChinaBridgeSupervisor(app_config=config, workspace=workspace, transport=_BridgeTestTransport())


def test_supervisor_install_required_uses_dependency_stamp(tmp_path: Path) -> None:
    supervisor = _build_supervisor(tmp_path)
    node_modules = tmp_path / "subsystems" / "china_channels_host" / "node_modules"
    node_modules.mkdir(parents=True, exist_ok=True)

    assert supervisor._host_install_required() is True

    supervisor._mark_host_dependencies_installed("pnpm")
    assert supervisor._host_install_required() is False

    package_json = tmp_path / "subsystems" / "china_channels_host" / "package.json"
    package_json.write_text('{"name":"updated"}\n', encoding="utf-8")

    assert supervisor._host_install_required() is True


@pytest.mark.asyncio
async def test_supervisor_clears_running_state_when_host_exits_before_client_connects(tmp_path: Path, monkeypatch) -> None:
    supervisor = _build_supervisor(tmp_path)
    client_instances: list[object] = []

    class _FakeClient:
        def __init__(self, **_kwargs) -> None:
            self._stop = asyncio.Event()
            self.stopped = False
            client_instances.append(self)

        async def run_forever(self) -> None:
            await self._stop.wait()

        async def send_frame(self, _payload: dict) -> None:
            return None

        async def stop(self) -> None:
            self.stopped = True
            self._stop.set()

    class _ExitedProcess:
        def __init__(self) -> None:
            self.returncode = 23

        async def wait(self) -> int:
            return self.returncode

    async def _fake_ensure_host_build() -> Path:
        return tmp_path / "subsystems" / "china_channels_host" / "dist" / "index.js"

    async def _fake_spawn_process(_dist_entry: Path) -> None:
        supervisor._process = _ExitedProcess()
        supervisor._write_state(running=True, built=True, pid=4242)

    monkeypatch.setattr(supervisor_module, "ChinaBridgeClient", _FakeClient)
    monkeypatch.setattr(supervisor, "_ensure_host_build", _fake_ensure_host_build)
    monkeypatch.setattr(supervisor, "_spawn_process", _fake_spawn_process)

    task = asyncio.create_task(supervisor._run_loop())
    try:
        for _ in range(50):
            if client_instances:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("supervisor did not start the bridge client")

        for _ in range(50):
            if supervisor.state.running is False and supervisor.state.pid is None:
                break
            await asyncio.sleep(0.02)
        else:
            raise AssertionError("supervisor did not clear running state after host exit")

        assert client_instances and getattr(client_instances[0], "stopped", False) is True
        assert supervisor.state.connected is False
        assert supervisor.state.running is False
        assert supervisor.state.pid is None
        assert supervisor.state.last_error == "china bridge host exited (23)"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
