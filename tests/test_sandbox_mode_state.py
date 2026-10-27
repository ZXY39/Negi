"""档位意图存储、安装态探测与档位 API 的 gate。

契约见 ``docs/architecture/sandbox-execution-plane.md``。这些断言钉的是"开关真的生效"
与"没有 jail 后端时绝不发放 jailed"，不是实现细节。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import main  # noqa: F401  (production import order)
from g3ku.runtime.session_trust import TRUST_THIRD_PARTY_GROUP
from g3ku.sandbox import state as sandbox_state
from g3ku.sandbox.policy import (
    JAIL_BACKENDS_AVAILABLE,
    MODE_RESTRICTED,
    MODE_UNRESTRICTED,
    TIER_HOST_FULL,
    TIER_JAILED,
    TIER_READONLY,
    resolve_tier,
    session_policy,
)
from g3ku.sandbox.probe import REQUIRED_DIMENSIONS, probe_install_state


@pytest.fixture()
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    store = tmp_path / "sandbox" / "session-modes.json"
    monkeypatch.setattr(sandbox_state, "_states_path", lambda: store)
    sandbox_state.reset_sandbox_state_for_tests()
    try:
        yield store
    finally:
        sandbox_state.reset_sandbox_state_for_tests()


@pytest.fixture()
def registry(tmp_path: Path):
    from g3ku.runtime.external_sessions import (
        get_external_session_registry,
        reset_external_session_registry,
    )

    reset_external_session_registry()
    reg = get_external_session_registry(tmp_path / "workspace")
    try:
        yield reg
    finally:
        reset_external_session_registry()


def test_mode_persists_and_reloads(isolated_state: Path) -> None:
    sandbox_state.set_session_mode("web:ceo-1", MODE_RESTRICTED)
    assert sandbox_state.get_session_mode("web:ceo-1") == MODE_RESTRICTED

    payload = json.loads(isolated_state.read_text(encoding="utf-8"))
    assert payload["sessions"]["web:ceo-1"] == MODE_RESTRICTED

    # 清缓存后从磁盘重读（模拟进程重启）
    sandbox_state.reset_sandbox_state_for_tests()
    assert sandbox_state.get_session_mode("web:ceo-1") == MODE_RESTRICTED

    sandbox_state.clear_session_mode("web:ceo-1")
    assert sandbox_state.get_session_mode("web:ceo-1") == ""


def test_invalid_mode_is_rejected(isolated_state: Path) -> None:
    with pytest.raises(ValueError):
        sandbox_state.set_session_mode("web:ceo-1", "jailed")


def test_session_policy_honors_stored_mode(isolated_state: Path, registry) -> None:
    # owner 默认 unrestricted
    assert session_policy("web:ceo-1").tier == TIER_HOST_FULL
    sandbox_state.set_session_mode("web:ceo-1", MODE_RESTRICTED)
    policy = session_policy("web:ceo-1")
    assert policy.tier == TIER_READONLY
    assert policy.exec_requires_governed is True
    assert policy.tool_allowed("exec") is True
    assert policy.tool_allowed("filesystem_write") is False

    # 第三方默认 restricted；显式放成 unrestricted 后回宿主（口令 gate 在 API 层）
    entry, _ = registry.resolve_or_create(
        bridge_id="qq-official-1", external_key="qq:group:g1", chat_type="group"
    )
    assert session_policy(entry.session_key).tier == TIER_READONLY
    sandbox_state.set_session_mode(entry.session_key, MODE_UNRESTRICTED)
    unlocked = session_policy(entry.session_key)
    assert unlocked.tier == TIER_HOST_FULL
    assert unlocked.trust == TRUST_THIRD_PARTY_GROUP


def test_jailed_is_never_granted_without_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("G3KU_SANDBOX_ENABLE_JAIL", raising=False)
    assert JAIL_BACKENDS_AVAILABLE is False
    # 安装态说 ready，但闸门没开 → 仍然只读
    assert (
        resolve_tier(
            trust=TRUST_THIRD_PARTY_GROUP,
            mode=MODE_RESTRICTED,
            install_state="ready",
        )
        == TIER_READONLY
    )
    # 显式传 jail_available=True 才发放（装配侧测试用这个口子）
    assert (
        resolve_tier(
            trust=TRUST_THIRD_PARTY_GROUP,
            mode=MODE_RESTRICTED,
            install_state="ready",
            jail_available=True,
        )
        == TIER_JAILED
    )


def test_jail_gate_env_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from g3ku.sandbox.policy import jail_backends_available

    monkeypatch.delenv("G3KU_SANDBOX_ENABLE_JAIL", raising=False)
    assert jail_backends_available() is False
    monkeypatch.setenv("G3KU_SANDBOX_ENABLE_JAIL", "1")
    assert jail_backends_available() is True
    assert (
        resolve_tier(trust=TRUST_THIRD_PARTY_GROUP, mode=MODE_RESTRICTED, install_state="ready")
        == TIER_JAILED
    )


def test_factory_only_builds_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    from g3ku.sandbox import factory

    assert factory.build_sandbox_manager({"state": "not_installed"}) is None
    manager = factory.build_sandbox_manager(
        {
            "state": "ready",
            "backend": "bwrap",
            "enforcement": {
                "fs_read": "full",
                "fs_write": "full",
                "network": "full",
                "process": "full",
                "resource": "partial",
            },
        }
    )
    assert manager is not None
    assert manager.select(trust="third_party_group").backend is not None


def test_factory_configures_wsl_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    from g3ku.sandbox import factory

    manager = factory.build_sandbox_manager(
        {
            "state": "ready",
            "backend": "wsl_bwrap",
            "enforcement": {
                "fs_read": "full",
                "fs_write": "full",
                "network": "full",
                "process": "full",
                "resource": "partial",
            },
            "detail": {"wsl": "C:/Windows/System32/wsl.exe", "selected_distro": "Ubuntu"},
        }
    )
    assert manager is not None
    assert manager._wsl is not None
    assert manager._wsl.distro == "Ubuntu"


def test_probe_shape() -> None:
    reading = probe_install_state()
    assert reading["state"] in {"ready", "partial", "runtime_missing", "not_installed"}
    assert set(REQUIRED_DIMENSIONS).issubset(set(reading["enforcement"]))
    assert isinstance(reading["detail"], dict)


@pytest.mark.asyncio
async def test_sandbox_api_owner_and_third_party_gate(
    isolated_state: Path,
    registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from g3ku.runtime.api import sandbox as sandbox_api

    entry, _ = registry.resolve_or_create(
        bridge_id="qq-official-1", external_key="qq:group:g1", chat_type="group"
    )
    monkeypatch.setattr(sandbox_api, "_session_exists", lambda key: True)

    # owner 切档不需要口令
    result = await sandbox_api.patch_session_sandbox(
        "web:ceo-1", {"mode": MODE_RESTRICTED}
    )
    assert result["item"]["resolved_tier"] == TIER_READONLY
    assert result["item"]["unlock_required"] is False
    assert result["item"]["effective_from"] == "next_turn"

    # 第三方切 unrestricted：没口令 403
    with pytest.raises(Exception) as missing:
        await sandbox_api.patch_session_sandbox(
            entry.session_key, {"mode": MODE_UNRESTRICTED}
        )
    assert getattr(missing.value, "status_code", None) == 403

    # 错口令 403
    monkeypatch.setattr(sandbox_api, "_verify_unlock_password", lambda pw: False)
    with pytest.raises(Exception) as invalid:
        await sandbox_api.patch_session_sandbox(
            entry.session_key, {"mode": MODE_UNRESTRICTED, "unlock_password": "nope"}
        )
    assert getattr(invalid.value, "status_code", None) == 403
    assert sandbox_state.get_session_mode(entry.session_key) == ""

    # 对口令通过，并留下操作员痕迹
    monkeypatch.setattr(sandbox_api, "_verify_unlock_password", lambda pw: pw == "right")
    ok = await sandbox_api.patch_session_sandbox(
        entry.session_key, {"mode": MODE_UNRESTRICTED, "unlock_password": "right"}
    )
    assert ok["item"]["resolved_tier"] == TIER_HOST_FULL
    assert ok["item"]["operator_unlock"]["at"]
    assert sandbox_state.get_session_mode(entry.session_key) == MODE_UNRESTRICTED

    # 非法 mode → 400
    with pytest.raises(Exception) as bad:
        await sandbox_api.patch_session_sandbox("web:ceo-1", {"mode": "root"})
    assert getattr(bad.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_sandbox_status_reports_no_file_management_without_backend(
    isolated_state: Path,
) -> None:
    from g3ku.runtime.api import sandbox as sandbox_api

    payload = await sandbox_api.get_sandbox_status(refresh=1)
    item = payload["item"]
    assert item["install_state"] in {"ready", "partial", "runtime_missing", "not_installed"}
    if item["install_state"] not in {"ready", "partial"}:
        assert item["file_management_available"] is False
        assert item["ui_labels"]["restricted"] == "只读"
    assert item["install_available"] is False
