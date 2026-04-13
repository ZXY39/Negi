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

