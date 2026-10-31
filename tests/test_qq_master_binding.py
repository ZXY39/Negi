"""QQ Master 一次性绑定码：内存注册表与配置写入。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import g3ku.runtime.external_sessions as external_sessions
from g3ku.runtime import qq_master_binding as binding


@pytest.fixture(autouse=True)
def _reset_binding_registry():
    binding.reset_qq_master_binding_registry()
    yield
    binding.reset_qq_master_binding_registry()


def test_one_active_code_per_app_and_exact_match() -> None:
    registry = binding.get_qq_master_binding_registry()
    first = registry.issue("111")
    assert first["token"].startswith("NEGI-MASTER-")
    assert registry.status("111")["pending"] is True
    assert registry.has_active("111")
    assert registry.match("111", first["token"]) is True
    assert registry.match("111", f"  {first['token'].lower()}  ") is True
    assert registry.match("222", first["token"]) is False
    assert registry.match("111", f"{first['token']} 额外内容") is False

    second = registry.issue("111")
    assert registry.match("111", first["token"]) is False
    assert registry.match("111", second["token"]) is True
    assert registry.consume("111") is True
    assert registry.consume("111") is False
    assert registry.status("111")["pending"] is False


def test_code_expires_after_ttl(monkeypatch) -> None:
    clock = {"now": 100.0}
    monkeypatch.setattr(
        binding, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )
    registry = binding.get_qq_master_binding_registry()
    issued = registry.issue("111")
    clock["now"] += binding.BINDING_TTL_SECONDS - 1
    assert registry.has_active("111") is True
    assert registry.match("111", issued["token"]) is True
    clock["now"] += 2
    assert registry.has_active("111") is False
    assert registry.match("111", issued["token"]) is False


def test_app_id_comes_from_the_account_scoped_bridge() -> None:
    assert binding.app_id_for_bridge("qq-official-1903529517") == "1903529517"
    assert binding.app_id_for_bridge("qq-official") == ""
    assert binding.app_id_for_bridge("qoder") == ""


@pytest.mark.asyncio
async def test_complete_binding_writes_owner_and_upgrades_matching_dms(
    tmp_path, monkeypatch
) -> None:
    from tests.resources.test_qq_official_admin import _write_config

    workspace = tmp_path / "workspace"
    _write_config(
        workspace,
        {"enabled": True, "accounts": {"111": {"appSecret": "", "enabled": True}}},
    )
    monkeypatch.chdir(workspace)

    class _FakeRegistry:
        def __init__(self) -> None:
            self.dm = SimpleNamespace(
                session_key="ext:qq-official-111:dm",
                bridge_id="qq-official-111",
                external_key="qq:c2c:EB6C8D4341C1238A627FF73CBE540DAE",
                chat_type="dm",
            )
            self.group = SimpleNamespace(
                session_key="ext:qq-official-111:group",
                bridge_id="qq-official-111",
                external_key="qq:group:g1",
                chat_type="group",
            )
            self.trusted: list[str] = []

        def list_bridge_sessions(self, bridge_id: str):
            return [self.dm, self.group] if bridge_id == "qq-official-111" else []

        def trust_owner(self, session_key: str):
            self.trusted.append(session_key)
            return SimpleNamespace(session_key=session_key)

    fake = _FakeRegistry()
    monkeypatch.setattr(
        external_sessions, "get_external_session_registry", lambda *args, **kwargs: fake
    )

    async def _noop_refresh(reason: str) -> None:
        return None

    monkeypatch.setattr(binding, "_refresh_after_save", _noop_refresh)
    binding.get_qq_master_binding_registry().issue("111")

    result = await binding.complete_binding(
        "111",
        "EB6C8D4341C1238A627FF73CBE540DAE",
        session_key="ext:qq-official-111:dm",
    )

    assert result["registered"] is True
    assert result["dm_sessions_upgraded"] == 1
    assert fake.trusted == ["ext:qq-official-111:dm"]
    from g3ku.config.loader import load_config

    owners = load_config().qq_bot.accounts["111"].owner_openids
    assert owners == ["EB6C8D4341C1238A627FF73CBE540DAE"]
    assert binding.get_qq_master_binding_registry().has_active("111") is False


@pytest.mark.asyncio
async def test_unregister_clears_owners_and_downgrades_dm_trust(tmp_path, monkeypatch) -> None:
    from tests.resources.test_qq_official_admin import _write_config

    workspace = tmp_path / "workspace"
    _write_config(
        workspace,
        {
            "enabled": True,
            "accounts": {
                "111": {
                    "appSecret": "",
                    "enabled": True,
                    "ownerOpenids": ["EB6C8D4341C1238A627FF73CBE540DAE"],
                }
            },
        },
    )
    monkeypatch.chdir(workspace)

    class _FakeRegistry:
        def __init__(self) -> None:
            self.cleared: list[str] = []

        def clear_owner_trust(self, bridge_id: str) -> int:
            self.cleared.append(bridge_id)
            return 1

    fake = _FakeRegistry()
    monkeypatch.setattr(
        external_sessions, "get_external_session_registry", lambda *args, **kwargs: fake
    )

    async def _noop_refresh(reason: str) -> None:
        return None

    monkeypatch.setattr(binding, "_refresh_after_save", _noop_refresh)
    binding.get_qq_master_binding_registry().issue("111")

    result = await binding.unregister_master("111")

    assert result["cleared_owner_count"] == 1
    assert result["dm_sessions_downgraded"] == 1
    assert fake.cleared == ["qq-official-111"]
    assert binding.get_qq_master_binding_registry().has_active("111") is False
    from g3ku.config.loader import load_config

    assert load_config().qq_bot.accounts["111"].owner_openids == []
