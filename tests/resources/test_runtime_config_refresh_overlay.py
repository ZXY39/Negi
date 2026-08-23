from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import g3ku.runtime.config_refresh as config_refresh
from g3ku.security.bootstrap import SecretOverlayStore, get_bootstrap_security_service


def test_bootstrap_security_service_reload_overlay_from_disk_refreshes_cache(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    service = get_bootstrap_security_service(workspace)
    service.setup_initial_realm(password="test-password")
    service.set_overlay_values(
        {
            "llm_config.demo.auth": {
                "type": "api_key",
                "api_key": "old-key",
            }
        }
    )

    master_key = service.active_master_key()
    assert master_key

    store = SecretOverlayStore(workspace)
    store.save(
        master_key=master_key,
        payload={
            "llm_config.demo.auth": {
                "type": "api_key",
                "api_key": "new-key",
            }
        },
    )

    assert service.get_overlay_value("llm_config.demo.auth")["api_key"] == "old-key"

    reloaded = service.reload_overlay_from_disk()

    assert reloaded is True
    assert service.get_overlay_value("llm_config.demo.auth")["api_key"] == "new-key"


def test_refresh_loop_runtime_config_reloads_security_overlay_before_refresh(monkeypatch) -> None:
    calls: list[str] = []

    class _Security:
        def reload_overlay_from_disk(self) -> bool:
            calls.append("reloaded")
            return True

    config = SimpleNamespace(
        get_role_model_target=lambda _role: ("custom", "demo-model"),
        agents=SimpleNamespace(
            defaults=SimpleNamespace(
                temperature=0.2,
                max_tokens=1024,
                reasoning_effort="medium",
            ),
            multi_agent=SimpleNamespace(),
        ),
        get_role_max_iterations=lambda _role: 6,
        resolve_role_model_key=lambda _role: "demo-key",
    )
    provider = object()
    loop = SimpleNamespace(_runtime_model_revision=0)

    monkeypatch.setattr(
        config_refresh,
        "get_runtime_config",
        lambda force=False: (config, 7, True),
    )
    monkeypatch.setattr(config_refresh, "build_chat_model", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(
        config_refresh,
        "get_bootstrap_security_service",
        lambda *_args, **_kwargs: _Security(),
        raising=False,
    )

    changed = config_refresh.refresh_loop_runtime_config(loop, force=True, reason="test")

    assert changed is True
    assert calls == ["reloaded"]
    assert loop.provider is provider
    assert loop.model_client is provider
    assert loop._runtime_model_revision == 7


def test_refresh_loop_runtime_config_threads_force_memory_sync(monkeypatch) -> None:
    config = SimpleNamespace(
        get_role_model_target=lambda _role: ("custom", "demo-model"),
        agents=SimpleNamespace(
            defaults=SimpleNamespace(
                temperature=0.2,
                max_tokens=1024,
                reasoning_effort="medium",
            ),
            multi_agent=SimpleNamespace(),
        ),
        get_role_max_iterations=lambda _role: 6,
        resolve_role_model_key=lambda _role: "demo-key",
    )
    provider = object()

    def _make_loop() -> tuple[SimpleNamespace, list[dict]]:
        sync_calls: list[dict] = []
        bootstrap = SimpleNamespace(
            sync_internal_tool_runtimes=lambda force=False, reason="runtime": sync_calls.append(
                {"force": force, "reason": reason}
            )
        )
        loop = SimpleNamespace(_runtime_model_revision=0, _bootstrap=bootstrap)
        return loop, sync_calls

    monkeypatch.setattr(
        config_refresh,
        "get_runtime_config",
        lambda force=False: (config, 7, True),
    )
    monkeypatch.setattr(config_refresh, "build_chat_model", lambda *_args, **_kwargs: provider)
    monkeypatch.setattr(
        config_refresh,
        "get_bootstrap_security_service",
        lambda *_args, **_kwargs: SimpleNamespace(reload_overlay_from_disk=lambda: True),
        raising=False,
    )

    # Default is fingerprint-gated: the memory runtime sync is NOT forced, so
    # a routine refresh does not close the active checkpointer.
    loop, sync_calls = _make_loop()
    config_refresh.refresh_loop_runtime_config(loop, force=True, reason="test")
    assert sync_calls == [{"force": False, "reason": "test"}]

    # Memory-affecting admin saves opt into the forced reset explicitly.
    loop, sync_calls = _make_loop()
    config_refresh.refresh_loop_runtime_config(
        loop, force=True, reason="admin_llm_memory_update", force_memory_sync=True
    )
    assert sync_calls == [{"force": True, "reason": "admin_llm_memory_update"}]


async def test_model_config_refresh_keeps_memory_sync_gated(monkeypatch) -> None:
    from g3ku.agent.tools.model_config import ModelConfigTool

    refresh_calls: list[dict] = []
    loop = SimpleNamespace(main_task_service=None)

    async def _capture_refresh(**kwargs):
        refresh_calls.append(kwargs)
        return True

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _capture_refresh)

    await ModelConfigTool()._refresh_runtime({"__g3ku_runtime": {"loop": loop}})

    # force_memory_sync stays False: the memory runtime reset is
    # fingerprint-gated so the active checkpointer survives the turn.
    assert refresh_calls == [
        {"force": True, "reason": "model_config_tool", "force_memory_sync": False}
    ]


async def test_model_config_refresh_falls_back_to_loop_when_project_locked(monkeypatch) -> None:
    from g3ku.agent.tools.model_config import ModelConfigTool

    fallback_calls: list[tuple] = []
    loop = SimpleNamespace(main_task_service=None)

    async def _locked_refresh(**kwargs):
        _ = kwargs
        raise RuntimeError("project is locked")

    monkeypatch.setattr("g3ku.shells.web.refresh_web_agent_runtime", _locked_refresh)
    monkeypatch.setattr(
        config_refresh,
        "refresh_loop_runtime_config",
        lambda target, **kwargs: fallback_calls.append((target, kwargs)),
    )

    # Must not raise: locked shells (e.g. the local CLI) refresh the
    # executing loop directly instead of failing the tool call.
    await ModelConfigTool()._refresh_runtime({"__g3ku_runtime": {"loop": loop}})

    assert len(fallback_calls) == 1
    target, kwargs = fallback_calls[0]
    assert target is loop
    assert kwargs["force"] is True
    assert kwargs["reason"] == "model_config_tool"
    assert kwargs["force_memory_sync"] is False


async def test_model_config_migrate_legacy_forces_memory_sync(monkeypatch) -> None:
    from g3ku.agent.tools.model_config import ModelConfigTool
    from g3ku.config.model_manager import ModelManager

    refresh_calls: list[dict] = []
    loop = SimpleNamespace(main_task_service=None)

    async def _capture_refresh(self, kwargs, *, force_memory_sync: bool = False):
        _ = kwargs
        refresh_calls.append({"force_memory_sync": force_memory_sync})

    monkeypatch.setattr(ModelConfigTool, "_refresh_runtime", _capture_refresh)
    monkeypatch.setattr(ModelManager, "load", staticmethod(lambda: SimpleNamespace()))
    monkeypatch.setattr(
        "g3ku.config.loader.load_config",
        lambda **_kwargs: SimpleNamespace(workspace_path="ws"),
    )

    await ModelConfigTool().execute("migrate_legacy", __g3ku_runtime={"loop": loop})

    # Legacy migration can rewrite memory bindings stored outside the
    # fingerprint tree, so it must force the memory runtime sync.
    assert refresh_calls == [{"force_memory_sync": True}]

