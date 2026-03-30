from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import g3ku_bootstrap
import g3ku.china_bridge.supervisor as supervisor_module
from g3ku.china_bridge.supervisor import ChinaBridgeSupervisor
from g3ku.config.loader import (
    _migrate_config,
    build_runtime_config_payload,
    ensure_startup_config_ready,
    load_config,
    save_config,
)
from g3ku.security import get_bootstrap_security_service


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
                    "nodeDispatchConcurrency": {"execution": 8, "inspection": 4},
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
                    "nodeDispatchConcurrency": {"execution": 8, "inspection": 4},
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


def _prepare_runtime_config_with_qqbot_secret_overlay(workspace: Path, monkeypatch) -> object:
    workspace.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(workspace)
    ensure_startup_config_ready()

    cfg = load_config()
    cfg.china_bridge.enabled = True
    cfg.china_bridge.auto_start = True
    cfg.china_bridge.node_bin = "node"
    cfg.china_bridge.npm_client = "pnpm"
    cfg.china_bridge.channels.qqbot.enabled = True
    cfg.china_bridge.channels.qqbot.app_id = "1903529517"
    save_config(cfg)

    security = get_bootstrap_security_service(workspace)
    security.setup_initial_realm(password="test-password")
    security.set_overlay_values(
        {"config.chinaBridge.channels.qqbot.clientSecret": "overlay-secret"}
    )
    return load_config()


def test_build_runtime_config_payload_applies_china_bridge_secret_overlay(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    cfg = _prepare_runtime_config_with_qqbot_secret_overlay(workspace, monkeypatch)

    runtime_payload = build_runtime_config_payload(cfg)
    saved_payload = json.loads((workspace / ".g3ku" / "config.json").read_text(encoding="utf-8"))

    assert (
        saved_payload["chinaBridge"]["channels"]["qqbot"].get("clientSecret", "") == ""
    )
    assert runtime_payload["chinaBridge"]["channels"]["qqbot"]["clientSecret"] == "overlay-secret"
    assert runtime_payload["chinaBridge"]["channels"]["qqbot"]["appId"] == "1903529517"


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


@pytest.mark.asyncio
async def test_supervisor_spawn_process_uses_resolved_runtime_config(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    cfg = _prepare_runtime_config_with_qqbot_secret_overlay(workspace, monkeypatch)
    dist_entry = workspace / "subsystems" / "china_channels_host" / "dist" / "index.js"
    dist_entry.parent.mkdir(parents=True, exist_ok=True)
    dist_entry.write_text("export {};\n", encoding="utf-8")

    supervisor = ChinaBridgeSupervisor(
        app_config=cfg,
        workspace=workspace,
        transport=_BridgeTestTransport(),
    )
    captured: dict[str, object] = {}

    class _FakeProcess:
        pid = 4321
        returncode = 0

        async def wait(self) -> int:
            return self.returncode

    async def _fake_create_subprocess_exec(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess()

    monkeypatch.setattr(
        supervisor_module.asyncio,
        "create_subprocess_exec",
        _fake_create_subprocess_exec,
    )
    monkeypatch.setattr(
        supervisor_module, "assign_process_to_kill_on_close_job", lambda _process: None
    )

    await supervisor._spawn_process(dist_entry)
    supervisor._close_host_log_handles()

    args = captured["args"]
    assert isinstance(args, tuple)
    assert args[2] == "--config"

    host_config_path = Path(str(args[3]))
    payload = json.loads(host_config_path.read_text(encoding="utf-8"))

    assert host_config_path == (
        workspace / ".g3ku" / "china-bridge" / "host.config.json"
    ).resolve()
    assert host_config_path != (workspace / ".g3ku" / "config.json").resolve()
    assert payload["chinaBridge"]["channels"]["qqbot"]["clientSecret"] == "overlay-secret"
    assert payload["chinaBridge"]["channels"]["qqbot"]["appId"] == "1903529517"
    assert supervisor.state.pid == 4321


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

def test_bootstrap_preflight_warns_when_node_and_package_manager_missing(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku" / "config.json").write_text(
        json.dumps(
            {
                "chinaBridge": {
                    "enabled": True,
                    "autoStart": True,
                    "nodeBin": "node",
                    "npmClient": "pnpm",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(g3ku_bootstrap, "PROJECT_ROOT", workspace)
    monkeypatch.setattr(g3ku_bootstrap, "_resolve_node_executable", lambda _config: None)
    monkeypatch.setattr(g3ku_bootstrap, "_node_satisfies_min_version", lambda _path: False)
    monkeypatch.setattr(g3ku_bootstrap, "_resolve_package_manager_executable", lambda _config, *, node_path=None: None)

    messages = g3ku_bootstrap._china_bridge_preflight_messages()

    assert len(messages) == 2
    assert "Node.js is not available in PATH" in messages[0]
    assert "no package manager was found" in messages[1]


def test_bootstrap_preflight_skips_when_china_bridge_is_disabled(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku" / "config.json").write_text(
        json.dumps({"chinaBridge": {"enabled": False, "autoStart": True}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(g3ku_bootstrap, "PROJECT_ROOT", workspace)

    assert g3ku_bootstrap._china_bridge_preflight_messages() == []

@pytest.mark.asyncio
async def test_supervisor_skips_start_when_prerequisites_are_missing(tmp_path: Path, monkeypatch) -> None:
    supervisor = _build_supervisor(tmp_path)

    monkeypatch.setattr(supervisor_module.shutil, "which", lambda _name: None)

    await supervisor.start()

    assert supervisor._runner_task is None
    assert supervisor.state.running is False
    assert supervisor.state.connected is False
    assert supervisor.state.built is True
    assert supervisor.state.pid is None
    assert supervisor.state.last_error == ""

def test_bootstrap_attempts_windows_node_install_when_missing(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku").mkdir(parents=True, exist_ok=True)
    (workspace / ".g3ku" / "config.json").write_text(
        json.dumps(
            {
                "chinaBridge": {
                    "enabled": True,
                    "autoStart": True,
                    "nodeBin": "node",
                    "npmClient": "pnpm",
                }
            }
        ),
        encoding="utf-8",
    )

    installed = {"done": False}
    node_path = workspace / "nodejs" / "node.exe"
    npm_path = workspace / "nodejs" / "npm.cmd"
    added_paths: list[Path] = []

    monkeypatch.setattr(g3ku_bootstrap, "PROJECT_ROOT", workspace)
    monkeypatch.setattr(g3ku_bootstrap.os, "name", "nt", raising=False)

    def _resolve_node(_config: dict):
        return node_path if installed["done"] else None

    def _install() -> bool:
        installed["done"] = True
        return True

    monkeypatch.setattr(g3ku_bootstrap, "_resolve_node_executable", _resolve_node)
    monkeypatch.setattr(g3ku_bootstrap, "_node_satisfies_min_version", lambda path: path == node_path)
    monkeypatch.setattr(g3ku_bootstrap, "_install_windows_node_lts", _install)
    monkeypatch.setattr(g3ku_bootstrap, "_resolve_package_manager_executable", lambda _config, *, node_path=None: npm_path if installed["done"] else None)
    monkeypatch.setattr(g3ku_bootstrap, "_ensure_executable_dir_on_path", lambda executable: added_paths.append(Path(executable).parent))

    g3ku_bootstrap._ensure_china_bridge_toolchain()

    assert installed["done"] is True
    assert added_paths == [node_path.parent, npm_path.parent]

