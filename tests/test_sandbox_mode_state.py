"""档位意图存储、安装态探测与档位 API 的 gate。

契约见 ``docs/architecture/sandbox-execution-plane.md``。这些断言钉的是"开关真的生效"
与"没有 jail 后端时绝不发放 jailed"，不是实现细节。
"""

from __future__ import annotations

import json
import time
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
    TIER_LIMITED,
    jail_backends_available,
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
    assert policy.tier == TIER_LIMITED
    assert policy.exec_requires_governed is False
    assert policy.tool_allowed("exec") is False
    assert policy.tool_allowed("filesystem_write") is True

    # 装了沙箱不会自动接管：受限档默认停在 limited，显式意图才带上 sandbox_enabled
    sandbox_state.set_session_sandbox("web:ceo-1", True)
    intent = session_policy("web:ceo-1")
    assert intent.sandbox_enabled is True
    assert intent.tier == TIER_LIMITED  # 闸门默认关 → 沙箱失效，仍走宿主受限
    sandbox_state.set_session_sandbox("web:ceo-1", False)

    # 第三方默认 restricted；显式放成 unrestricted 后回宿主（口令 gate 在 API 层）
    entry, _ = registry.resolve_or_create(
        bridge_id="qq-official-1", external_key="qq:group:g1", chat_type="group"
    )
    assert session_policy(entry.session_key).tier == TIER_LIMITED
    sandbox_state.set_session_mode(entry.session_key, MODE_UNRESTRICTED)
    unlocked = session_policy(entry.session_key)
    assert unlocked.tier == TIER_HOST_FULL
    assert unlocked.trust == TRUST_THIRD_PARTY_GROUP


def test_jailed_is_never_granted_without_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("G3KU_SANDBOX_ENABLE_JAIL", raising=False)
    assert JAIL_BACKENDS_AVAILABLE is False
    # 安装态说 ready，但闸门没开 → 仍然受限，即使显式要求进沙箱
    assert (
        resolve_tier(
            trust=TRUST_THIRD_PARTY_GROUP,
            mode=MODE_RESTRICTED,
            install_state="ready",
            sandbox_enabled=True,
        )
        == TIER_LIMITED
    )
    # 闸门口子打开后，没有显式"进沙箱"意图也停在受限：装了不等于接管
    assert (
        resolve_tier(
            trust=TRUST_THIRD_PARTY_GROUP,
            mode=MODE_RESTRICTED,
            install_state="ready",
            jail_available=True,
        )
        == TIER_LIMITED
    )
    # 意图 + 后端可用才发放 jailed
    assert (
        resolve_tier(
            trust=TRUST_THIRD_PARTY_GROUP,
            mode=MODE_RESTRICTED,
            install_state="ready",
            jail_available=True,
            sandbox_enabled=True,
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
        == TIER_LIMITED
    )
    assert (
        resolve_tier(
            trust=TRUST_THIRD_PARTY_GROUP,
            mode=MODE_RESTRICTED,
            install_state="ready",
            sandbox_enabled=True,
        )
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


def test_writable_roots_are_normalized_and_owner_only(isolated_state: Path, registry) -> None:
    roots = sandbox_state.set_writable_roots(
        "web:ceo-1",
        ["/tmp/out", "/tmp/out", "relative/path", "  ", "/tmp/deliverables"],
    )
    assert roots == ["/tmp/out", "/tmp/deliverables"]
    assert sandbox_state.get_writable_roots("web:ceo-1") == roots

    # 清空即删除条目
    assert sandbox_state.set_writable_roots("web:ceo-1", []) == []
    assert sandbox_state.get_writable_roots("web:ceo-1") == []


def test_writable_roots_are_capped(isolated_state: Path) -> None:
    many = [f"/tmp/root-{index}" for index in range(40)]
    roots = sandbox_state.set_writable_roots("web:ceo-1", many)
    assert len(roots) == sandbox_state._MAX_WRITABLE_ROOTS


def test_third_party_patch_rejects_writable_roots(isolated_state: Path, registry) -> None:
    import asyncio

    from g3ku.runtime.api import sandbox as sandbox_api

    entry, _ = registry.resolve_or_create(
        bridge_id="qq-official-1", external_key="qq:group:g1", chat_type="group"
    )
    original = sandbox_api._session_exists
    sandbox_api._session_exists = lambda key: True
    try:
        with pytest.raises(Exception) as excinfo:
            asyncio.run(
                sandbox_api.patch_session_sandbox(
                    entry.session_key,
                    {"writable_roots": ["/tmp/x"]},
                )
            )
        assert getattr(excinfo.value, "status_code", None) == 403
    finally:
        sandbox_api._session_exists = original
    assert sandbox_state.get_writable_roots(entry.session_key) == []


def test_sandbox_persona_feature_is_gone(isolated_state: Path) -> None:
    assert not hasattr(sandbox_state, "sandbox_persona_text")
    assert not hasattr(sandbox_state, "set_sandbox_persona_text")


def test_third_party_persona_uses_the_single_local_persona(isolated_state: Path) -> None:
    """人设不再按会话隔离：第三方会话读到的就是 local 记忆域里的那份。"""
    from types import SimpleNamespace

    from g3ku.runtime.frontdoor.message_builder import adopt_persona

    class _Manager:
        def persona_text(self):
            return "统一人设"

    session = SimpleNamespace(state=SimpleNamespace(session_key="ext:unknown-bridge:abc"))
    assert adopt_persona(session, memory_manager=_Manager(), reason="test") == "统一人设"


@pytest.mark.asyncio
async def test_sandbox_tools_api_roundtrip(
    isolated_state: Path,
    registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from g3ku.runtime.api import sandbox as sandbox_api

    entry, _ = registry.resolve_or_create(
        bridge_id="qq-official-1", external_key="qq:group:g1", chat_type="group"
    )
    monkeypatch.setattr(sandbox_api, "_session_exists", lambda key: True)
    monkeypatch.setattr(sandbox_api, "_verify_unlock_password", lambda pw: pw == "right")

    got = await sandbox_api.get_session_sandbox_tools(entry.session_key)
    item = got["item"]
    assert item["applicable"] is True
    assert "web_fetch" in item["allowed_tools"]
    assert "search_session_history" in item["allowed_tools"]
    assert "exec" not in item["allowed_tools"]
    assert item["exec_allowed"] is False
    assert item["allowed_tools_customized"] is False
    default_tools = list(item["default_allowed_tools"])
    assert "search_session_history" in default_tools

    with pytest.raises(Exception) as missing:
        await sandbox_api.patch_session_sandbox_tools(
            entry.session_key, {"allowed_tools": ["exec"]}
        )
    assert getattr(missing.value, "status_code", None) == 403
    monkeypatch.setattr(sandbox_api, "_verify_unlock_password", lambda pw: False)
    with pytest.raises(Exception) as invalid:
        await sandbox_api.patch_session_sandbox_tools(
            entry.session_key, {"allowed_tools": ["exec"], "unlock_password": "nope"}
        )
    assert getattr(invalid.value, "status_code", None) == 403

    monkeypatch.setattr(sandbox_api, "_verify_unlock_password", lambda pw: pw == "right")
    saved = await sandbox_api.patch_session_sandbox_tools(
        entry.session_key,
        {"allowed_tools": ["exec", "web_fetch"], "unlock_password": "right"},
    )
    saved_item = saved["item"]
    assert saved_item["exec_allowed"] is True
    assert "exec" in saved_item["allowed_tools"]
    assert saved_item["allowed_tools_customized"] is True
    policy = session_policy(entry.session_key)
    assert policy.exec_allowed is True
    assert policy.exec_requires_governed is True
    stored = sandbox_state.get_session_allowed_tools(entry.session_key)
    assert "exec" in stored
    assert "silent" in stored  # 固定项自动补入

    restored = await sandbox_api.patch_session_sandbox_tools(
        entry.session_key,
        {"allowed_tools": default_tools, "unlock_password": "right"},
    )
    assert restored["item"]["allowed_tools_customized"] is False
    assert saved["item"]["effective_from"] == "next_turn"
    assert sandbox_state.get_session_allowed_tools(entry.session_key) == []

    owner = await sandbox_api.get_session_sandbox_tools("web:ceo-1")
    assert owner["item"]["applicable"] is False


def test_session_workspace_is_stable_and_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from g3ku.runtime.frontdoor import session_workspace

    monkeypatch.setattr(
        session_workspace, "data_work_path", lambda *parts: tmp_path.joinpath(*parts)
    )
    first = session_workspace.ceo_session_workspace_dir("ext:qq-official-1:abc", create=True)
    second = session_workspace.ceo_session_workspace_dir("ext:qq-official-1:abc", create=True)
    other = session_workspace.ceo_session_workspace_dir("ext:qq-official-1:def", create=True)
    assert first == second
    assert first != other
    assert Path(first).is_dir()
    assert Path(other).is_dir()


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

    # owner 开沙箱同样要口令（安全边界改变，不分信任级）
    with pytest.raises(Exception) as owner_missing:
        await sandbox_api.patch_session_sandbox(
            "web:ceo-1", {"mode": MODE_RESTRICTED}
        )
    assert getattr(owner_missing.value, "status_code", None) == 403
    assert sandbox_state.get_session_mode("web:ceo-1") == ""

    monkeypatch.setattr(sandbox_api, "_verify_unlock_password", lambda pw: pw == "right")
    result = await sandbox_api.patch_session_sandbox(
        "web:ceo-1", {"mode": MODE_RESTRICTED, "unlock_password": "right"}
    )
    assert result["item"]["resolved_tier"] == TIER_LIMITED
    assert result["item"]["unlock_required"] is True
    assert result["item"]["effective_from"] == "next_turn"
    assert result["item"]["operator_unlock"]["at"]

    # 意图没变（只重存同一档）不走口令闸
    same = await sandbox_api.patch_session_sandbox(
        "web:ceo-1", {"mode": MODE_RESTRICTED}
    )
    assert same["item"]["resolved_tier"] == TIER_LIMITED

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

    # 非法 mode → 400（归一化先于口令校验）
    with pytest.raises(Exception) as bad:
        await sandbox_api.patch_session_sandbox("web:ceo-1", {"mode": "root"})
    assert getattr(bad.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_sandbox_intent_is_explicit_and_password_gated(
    isolated_state: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进入沙箱是逐会话动作：要口令、只接受布尔、非受限模式不许开、离开受限即清意图。"""
    from g3ku.runtime.api import sandbox as sandbox_api

    monkeypatch.setattr(sandbox_api, "_session_exists", lambda key: True)
    monkeypatch.setattr(sandbox_api, "install_state", lambda: {"state": "ready", "backend": "wsl_bwrap"})
    monkeypatch.setattr(sandbox_api, "_verify_unlock_password", lambda pw: pw == "right")
    key = "web:ceo-intent"

    await sandbox_api.patch_session_sandbox(key, {"mode": MODE_RESTRICTED, "unlock_password": "right"})

    with pytest.raises(Exception) as missing:
        await sandbox_api.patch_session_sandbox(key, {"sandbox": True})
    assert getattr(missing.value, "status_code", None) == 403
    assert sandbox_state.get_session_sandbox(key) is False

    entered = await sandbox_api.patch_session_sandbox(
        key, {"sandbox": True, "unlock_password": "right"}
    )
    item = entered["item"]
    assert item["sandbox_enabled"] is True
    # 闸门默认关：意图在、jail 没接住 → 跑受限档，UI 据此显示"沙箱失效"
    assert item["sandbox_failed"] is True
    assert item["runs_in_jail"] is False
    assert item["resolved_tier"] == TIER_LIMITED

    with pytest.raises(Exception) as bad_value:
        await sandbox_api.patch_session_sandbox(
            key, {"sandbox": "yes", "unlock_password": "right"}
        )
    assert getattr(bad_value.value, "status_code", None) == 400

    # 离开受限档会清掉沙箱意图，之后不许在 unrestricted 上补开
    left = await sandbox_api.patch_session_sandbox(
        key, {"mode": MODE_UNRESTRICTED, "unlock_password": "right"}
    )
    assert left["item"]["sandbox_enabled"] is False
    assert sandbox_state.get_session_sandbox(key) is False
    with pytest.raises(Exception) as wrong_mode:
        await sandbox_api.patch_session_sandbox(
            key, {"sandbox": True, "unlock_password": "right"}
        )
    assert getattr(wrong_mode.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_sandbox_status_reports_no_file_management_without_backend(
    isolated_state: Path,
) -> None:
    from g3ku.runtime.api import sandbox as sandbox_api

    payload = await sandbox_api.get_sandbox_status(refresh=1)
    item = payload["item"]
    assert item["install_state"] in {"ready", "partial", "runtime_missing", "not_installed"}
    if item["install_state"] not in {"ready", "partial"}:
        assert item["ui_labels"]["restricted"] == "受限"
    # 卷文件管理要安装态与 jail 闸门同时成立；只看安装态会广告一个永远列不出文件的面板。
    if item["install_state"] not in {"ready", "partial"} or not jail_backends_available():
        assert item["file_management_available"] is False
    assert item["install_available"] is False


def test_wsl_jail_rejects_writable_roots_it_cannot_mount(
    isolated_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WSL jail 挂不上宿主路径：这条承诺要在落盘前拒绝，而不是存下来静默失败。

    实机读数：PATCH 返回 200 并保存了 `E:/…`，jail 内每次写都回
    `host path is not reachable inside the sandbox`，交付目录始终是空的。
    """
    import asyncio
    from types import SimpleNamespace

    from g3ku.runtime.api import sandbox as sandbox_api

    monkeypatch.setattr(sandbox_api, "_session_exists", lambda key: True)
    monkeypatch.setattr(
        sandbox_api, "session_policy",
        lambda key: SimpleNamespace(trust="owner", mode="restricted", tier="jailed"),
    )
    monkeypatch.setattr(
        sandbox_api, "install_state", lambda: {"state": "ready", "backend": "wsl_bwrap"}
    )

    with pytest.raises(Exception) as excinfo:
        asyncio.run(
            sandbox_api.patch_session_sandbox(
                "web:ceo-wsl", {"mode": "restricted", "writable_roots": ["E:/delivery"]}
            )
        )
    assert getattr(excinfo.value, "status_code", None) == 400
    assert getattr(excinfo.value, "detail", "") == "writable_roots_unreachable_in_backend"
    assert sandbox_state.get_writable_roots("web:ceo-wsl") == []

    # 本地 bwrap 后端同一条根是可用的，不能被这条闸门一起拒掉。
    monkeypatch.setattr(sandbox_api, "install_state", lambda: {"state": "ready", "backend": "bwrap"})
    result = asyncio.run(
        sandbox_api.patch_session_sandbox("web:ceo-local", {"mode": "restricted", "writable_roots": ["/srv/delivery"]})
    )
    assert result["ok"] is True
    assert sandbox_state.get_writable_roots("web:ceo-local")


def test_install_state_within_ttl_does_not_reprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL 内不许起探测子进程：档位解析每回合都要读它，不能变成每回合跑 wsl.exe。"""
    sandbox_state.reset_sandbox_state_for_tests()
    calls: list[int] = []

    def fake_probe():
        calls.append(1)
        return {"state": "ready", "backend": "fake", "enforcement": {}, "detail": {}}

    monkeypatch.setattr(sandbox_state, "probe_install_state", fake_probe)
    monkeypatch.setattr(sandbox_state, "INSTALL_STATE_TTL_SECONDS", 3600.0)
    try:
        assert sandbox_state.install_state()["state"] == "ready"
        assert sandbox_state.install_state()["state"] == "ready"
        assert len(calls) == 1
    finally:
        sandbox_state.reset_sandbox_state_for_tests()


def test_install_state_reprobes_in_background_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """装好/修好或坏掉，最迟一个 TTL 后下一回合读到新结论；读取本身不等子进程。"""
    sandbox_state.reset_sandbox_state_for_tests()
    readings = [
        {"state": "ready", "backend": "fake", "enforcement": {}, "detail": {}},
        {"state": "not_installed", "backend": "", "enforcement": {}, "detail": {}},
    ]

    def flip_probe():
        # 第一次同步探测拿走 ready，之后都是 not_installed
        return readings.pop(0) if len(readings) > 1 else readings[0]

    monkeypatch.setattr(sandbox_state, "probe_install_state", flip_probe)
    monkeypatch.setattr(sandbox_state, "INSTALL_STATE_TTL_SECONDS", 0.0)
    try:
        assert sandbox_state.install_state()["state"] == "ready"
        # 过期读取先返回旧值（不阻塞），同时起后台重探。
        assert sandbox_state.install_state()["state"] == "ready"
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if sandbox_state.install_state()["state"] == "not_installed":
                break
            time.sleep(0.01)
        assert sandbox_state.install_state()["state"] == "not_installed"
    finally:
        sandbox_state.reset_sandbox_state_for_tests()
