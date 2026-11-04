from __future__ import annotations

from types import SimpleNamespace

from g3ku.runtime import qq_bot_identity, skill_roster_notice


def _set_identity(tmp_path, monkeypatch, nickname: str) -> None:
    monkeypatch.setattr(
        qq_bot_identity,
        "data_work_path",
        lambda *parts: tmp_path.joinpath(*parts),
    )
    qq_bot_identity.clear_qq_bot_identity_store()
    qq_bot_identity.set_qq_bot_identity(
        "qq-official-app", bot_id="BOTID", nickname=nickname
    )


def test_identity_store_roundtrip(tmp_path, monkeypatch) -> None:
    _set_identity(tmp_path, monkeypatch, "Negi")
    entry = qq_bot_identity.get_qq_bot_identity("qq-official-app")
    assert entry["nickname"] == "Negi"
    assert entry["bot_id"] == "BOTID"


def test_qq_identity_notice_initial_change_and_compaction(tmp_path, monkeypatch) -> None:
    _set_identity(tmp_path, monkeypatch, "Negi")
    monkeypatch.setattr(
        qq_bot_identity,
        "get_qq_bot_identity",
        lambda bridge_id: {"nickname": "Negi"},
    )
    from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps

    def provider(**kwargs):
        return CeoFrontDoorRuntimeOps._frontdoor_qq_identity_provider(
            SimpleNamespace(), session_key="ext:qq-official-app:qq:group:g1", **kwargs
        )

    scope = "session:ext:qq-official-app:qq:group:g1"
    skill_roster_notice.drop_notice_scope(scope)
    message, _ = skill_roster_notice.deliver_for_scope(
        scope, [], now="2026-11-05T10:00:00+08:00", qq_identity_provider=provider
    )
    assert message is not None
    assert "### QQ 身份" in message["content"]
    assert "你在 QQ 的昵称是「Negi」" in message["content"]

    again, _ = skill_roster_notice.deliver_for_scope(
        scope, [], now="2026-11-05T10:01:00+08:00", qq_identity_provider=provider
    )
    assert again is None

    monkeypatch.setattr(
        qq_bot_identity,
        "get_qq_bot_identity",
        lambda bridge_id: {"nickname": "Negi 2"},
    )
    changed, _ = skill_roster_notice.deliver_for_scope(
        scope, [], now="2026-11-05T10:02:00+08:00", qq_identity_provider=provider
    )
    assert changed is not None
    assert "已从「Negi」变为「Negi 2」" in changed["content"]

    skill_roster_notice.reset_contract_sections_for_compaction(scope)
    after_compaction, _ = skill_roster_notice.deliver_for_scope(
        scope, [], now="2026-11-05T10:03:00+08:00", qq_identity_provider=provider
    )
    assert after_compaction is not None
    assert "你在 QQ 的昵称是「Negi 2」" in after_compaction["content"]


def test_qq_identity_provider_ignores_non_qq_sessions(monkeypatch) -> None:
    from g3ku.runtime.frontdoor._ceo_runtime_ops import CeoFrontDoorRuntimeOps

    monkeypatch.setattr(
        qq_bot_identity,
        "get_qq_bot_identity",
        lambda bridge_id: {"nickname": "Negi"},
    )
    result = CeoFrontDoorRuntimeOps._frontdoor_qq_identity_provider(
        SimpleNamespace(), session_key="web:local:1"
    )
    assert result is None
