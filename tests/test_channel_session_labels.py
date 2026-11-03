"""渠道会话分类标题：QQ 官方桥按账号备注逐号解析，不把第一个号的备注套给所有号。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from g3ku.config.schema import ExternalApiTokenConfig, QqBotAccountConfig
from g3ku.runtime import web_ceo_sessions


def _install_config(monkeypatch, *, accounts, tokens=None) -> None:
    cfg = SimpleNamespace(
        qq_bot=SimpleNamespace(accounts=dict(accounts)),
        external_api=SimpleNamespace(tokens=dict(tokens or {})),
    )
    monkeypatch.setattr(web_ceo_sessions, "get_runtime_config", lambda: (cfg, 0, False))


def test_channel_label_uses_each_qq_accounts_own_remark(monkeypatch) -> None:
    _install_config(
        monkeypatch,
        accounts={
            "111": QqBotAccountConfig(app_secret="a", label="qq"),
            "222": QqBotAccountConfig(app_secret="b", label="第二个号"),
        },
    )

    assert web_ceo_sessions._channel_label("ext:qq-official-111") == "qq"
    assert web_ceo_sessions._channel_label("ext:qq-official-222") == "第二个号"


def test_channel_label_falls_back_to_qq_for_unknown_app_id(monkeypatch) -> None:
    _install_config(
        monkeypatch,
        accounts={"111": QqBotAccountConfig(app_secret="a", label="qq")},
    )

    assert web_ceo_sessions._channel_label("ext:qq-official-999") == "QQ"


def test_channel_label_legacy_bare_bridge_uses_the_first_remark(monkeypatch) -> None:
    _install_config(
        monkeypatch,
        accounts={"111": QqBotAccountConfig(app_secret="a", label="主号")},
    )

    assert web_ceo_sessions._channel_label("ext:qq-official") == "主号"


def test_catalog_groups_carry_each_qq_accounts_remark(monkeypatch, tmp_path: Path) -> None:
    from g3ku.runtime.external_sessions import (
        ExternalSessionRegistry,
        reset_external_session_registry,
    )

    _install_config(
        monkeypatch,
        accounts={
            "111": QqBotAccountConfig(app_secret="a", label="qq"),
            "222": QqBotAccountConfig(app_secret="b", label="第二个号"),
        },
        tokens={
            "qq-official-111": ExternalApiTokenConfig(token="t1", label="官方 QQ 机器人 111"),
            "qq-official-222": ExternalApiTokenConfig(token="t2", label="官方 QQ 机器人 222"),
        },
    )
    reset_external_session_registry()
    registry = ExternalSessionRegistry(tmp_path)
    first, _ = registry.resolve_or_create(
        bridge_id="qq-official-111", external_key="qq:c2c:peer-1"
    )
    second, _ = registry.resolve_or_create(
        bridge_id="qq-official-222", external_key="qq:c2c:peer-2"
    )

    class _Store:
        workspace = tmp_path

        def list_sessions(self):
            return [{"key": first.session_key}, {"key": second.session_key}]

        def has_transcript(self, _key: str) -> bool:
            return False

        def get_or_create(self, _key: str):
            raise AssertionError("registry-only rows must not materialize transcripts")

    catalog = web_ceo_sessions.build_ceo_session_catalog(_Store(), active_session_id="")
    labels = {group["channel_id"]: group["label"] for group in catalog["channel_groups"]}

    assert labels["ext:qq-official-111"] == "qq"
    assert labels["ext:qq-official-222"] == "第二个号"
    titles = {
        item["session_id"]: item["title"]
        for group in catalog["channel_groups"]
        for item in group["items"]
    }
    assert titles[first.session_key].startswith("qq · ")
    assert titles[second.session_key].startswith("第二个号 · ")
