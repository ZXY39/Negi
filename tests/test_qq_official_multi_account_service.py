"""一号一桥的服务注册表。

钉住三件在单号时代靠"重启蒙对"、多号并跑后必须显式成立的事：
按 AppID 建/收实例、密钥变更能触发重建、停用的号不起桥。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import g3ku.config.loader as config_loader
import g3ku.qq_official.service as qq_service
import g3ku.shells.web as web_shell
from g3ku.config.schema import QqBotAccountConfig, QqBotConfig
from g3ku.qq_official.messages import bridge_id_for_app_id


class _FakeService:
    instances: list["_FakeService"] = []

    def __init__(self, *, app_id: str, base_url: str | None = None):
        self.app_id = app_id
        self.bridge_id = bridge_id_for_app_id(app_id)
        self.sync_calls: list[tuple[bool, object]] = []
        self.stopped = 0
        _FakeService.instances.append(self)

    async def sync_from_config(self, *, account, global_enabled: bool) -> None:
        self.sync_calls.append((global_enabled, account))

    async def stop(self) -> None:
        self.stopped += 1

    def status(self) -> dict:
        return {"state": "connected", "detail": ""}


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch):
    _FakeService.instances = []
    web_shell._global_qq_official_services.clear()
    monkeypatch.setattr(qq_service, "QqOfficialService", _FakeService)
    yield web_shell
    web_shell._global_qq_official_services.clear()


def _stub_config(monkeypatch: pytest.MonkeyPatch, *, enabled: bool, accounts: dict) -> None:
    cfg = SimpleNamespace(qq_bot=QqBotConfig(enabled=enabled, accounts=accounts))
    monkeypatch.setattr(config_loader, "load_config", lambda *a, **k: cfg)


def _sync() -> None:
    asyncio.run(web_shell._sync_qq_official_service())


def test_registry_creates_one_service_per_account(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_config(
        monkeypatch,
        enabled=True,
        accounts={
            "111": QqBotAccountConfig(app_secret="a"),
            "222": QqBotAccountConfig(app_secret="b", sandbox=True),
        },
    )

    _sync()

    assert sorted(registry._global_qq_official_services) == ["qq-official-111", "qq-official-222"]
    statuses = {row["bridge_id"]: row for row in registry.qq_official_service_statuses()}
    assert statuses["qq-official-111"]["app_id"] == "111"
    assert statuses["qq-official-222"]["state"] == "connected"


def test_registry_stops_and_drops_removed_account(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_config(monkeypatch, enabled=True, accounts={"111": QqBotAccountConfig(app_secret="a")})
    _sync()
    kept = registry._global_qq_official_services["qq-official-111"]

    _stub_config(monkeypatch, enabled=True, accounts={"222": QqBotAccountConfig(app_secret="b")})
    _sync()

    assert kept.stopped == 1
    assert sorted(registry._global_qq_official_services) == ["qq-official-222"]


def test_registry_survives_one_account_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    class Exploding(_FakeService):
        async def sync_from_config(self, *, account, global_enabled: bool) -> None:
            if self.app_id == "boom":
                raise RuntimeError("登录失败")

    monkeypatch.setattr(qq_service, "QqOfficialService", Exploding)
    _stub_config(
        monkeypatch,
        enabled=True,
        accounts={"boom": QqBotAccountConfig(app_secret="a"), "fine": QqBotAccountConfig(app_secret="b")},
    )

    _sync()

    # 单号异常不得连带别的号不建桥，也不得把注册表留成半套。
    assert sorted(web_shell._global_qq_official_services) == ["qq-official-boom", "qq-official-fine"]


def test_global_disable_reaches_every_account(registry, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_config(monkeypatch, enabled=True, accounts={"111": QqBotAccountConfig(app_secret="a")})
    _sync()
    _stub_config(monkeypatch, enabled=False, accounts={"111": QqBotAccountConfig(app_secret="a")})
    _sync()

    service = registry._global_qq_official_services["qq-official-111"]
    assert [call[0] for call in service.sync_calls] == [True, False]


def test_secret_change_moves_the_restart_signature() -> None:
    """签名不含密钥时，改过 AppSecret 的号会永远停在旧凭证的重试循环里。"""
    def sig(secret: str) -> str:
        return qq_service.QqOfficialService._signature(
            "111", QqBotAccountConfig(app_secret=secret), global_enabled=True
        )

    assert sig("a") == sig("a")
    assert sig("a") != sig("b")
    assert sig("") != sig("a")


def test_account_without_secret_never_starts_a_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []

    async def fake_restart(self, account) -> None:
        started.append(self.bridge_id)

    monkeypatch.setattr(qq_service.QqOfficialService, "_restart", fake_restart)
    monkeypatch.setattr(qq_service, "load_config", lambda *a, **k: SimpleNamespace(web=SimpleNamespace(port=1)))

    service = qq_service.QqOfficialService(app_id="111")
    asyncio.run(service.sync_from_config(account=QqBotAccountConfig(app_secret="  "), global_enabled=True))

    assert started == []
    assert service.status()["state"] == "not_configured"
    assert "AppSecret" in service.status()["detail"]


def test_disabled_account_stops_its_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qq_service, "load_config", lambda *a, **k: SimpleNamespace(web=SimpleNamespace(port=1)))
    service = qq_service.QqOfficialService(app_id="111")
    asyncio.run(service.sync_from_config(account=QqBotAccountConfig(app_secret="a", enabled=False), global_enabled=True))

    assert service.status()["state"] == "account_disabled"
