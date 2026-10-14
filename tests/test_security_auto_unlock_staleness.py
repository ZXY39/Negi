"""A config-bundle import swaps the master key and the secret overlay as a pair, but the
auto-unlock credential is deliberately not part of the bundle. Left behind, it is a bearer
file for the *previous* key, and the startup unlock order reads it before the password — so
the project comes up "unlocked" holding a key that cannot open its own overlay: every secret
reads empty and every write is refused, while nothing anywhere says so.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from g3ku.security.bootstrap import BootstrapSecurityService, SecretOverlayStore


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


def _auto_unlock_path(workspace):
    return workspace / ".g3ku" / "llm-config" / "auto-unlock.key"


def _overlay_path(workspace):
    return workspace / ".g3ku" / "secret-realms" / "default.enc"


def _store(workspace) -> SecretOverlayStore:
    return SecretOverlayStore(workspace)


def _install_second_key(workspace, *, password: str) -> str:
    """What bundle import does: put the matching overlay down first, then adopt the key."""
    fresh_key = Fernet.generate_key().decode("utf-8")
    _store(workspace).save(master_key=fresh_key, payload={"config.providers.openai.apiKey": "imported-key"})
    service = BootstrapSecurityService(workspace)
    service.install_master_key(master_key=fresh_key, password=password)
    return fresh_key


def test_import_rotates_a_stale_auto_unlock_credential(workspace) -> None:
    service = BootstrapSecurityService(workspace)
    service.setup_initial_realm(password="owner-password")
    service.set_overlay_values({"config.providers.openai.apiKey": "old-key"})
    service.set_auto_unlock(enabled=True)
    stale_key = _auto_unlock_path(workspace).read_text(encoding="utf-8").strip()
    assert stale_key

    installed_key = _install_second_key(workspace, password="bundle-password")

    refreshed = _auto_unlock_path(workspace).read_text(encoding="utf-8").strip()
    assert refreshed == installed_key
    assert refreshed != stale_key

    # 重启形状：新进程只会用 auto-unlock 文件，它必须解得开盘上的覆盖层。
    restarted = BootstrapSecurityService(workspace)
    status = restarted.activate_with_master_key(master_key=restarted.auto_unlock_master_key())
    assert status["overlay_readable"] is True
    assert restarted.current_overlay() == {"config.providers.openai.apiKey": "imported-key"}


def test_import_leaves_auto_unlock_off_when_it_was_off(workspace) -> None:
    service = BootstrapSecurityService(workspace)
    service.setup_initial_realm(password="owner-password")
    assert not _auto_unlock_path(workspace).exists()

    _install_second_key(workspace, password="bundle-password")

    # 免密启动是操作员的选择，导入不该替他打开。
    assert not _auto_unlock_path(workspace).exists()


def test_status_names_an_overlay_the_active_key_cannot_open(workspace) -> None:
    service = BootstrapSecurityService(workspace)
    service.setup_initial_realm(password="owner-password")
    service.set_overlay_values({"config.providers.openai.apiKey": "precious-key"})
    assert service.status()["overlay_readable"] is True
    service.lock()
    assert service.status()["overlay_readable"] is None

    intruder = BootstrapSecurityService(workspace)
    status = intruder.activate_with_master_key(master_key=Fernet.generate_key().decode("utf-8"))

    assert status["mode"] == "unlocked"
    assert status["overlay_readable"] is False
    with pytest.raises(ValueError, match="undecryptable"):
        intruder.set_overlay_values({"config.providers.openai.apiKey": ""})


def test_readable_overlay_entries_are_reported_as_a_count(workspace) -> None:
    service = BootstrapSecurityService(workspace)
    service.setup_initial_realm(password="owner-password")
    service.set_overlay_values(
        {
            "config.providers.openai.apiKey": "one",
            "llm_config.abc.auth.api_key": "two",
        }
    )

    status = service.status()

    assert status["overlay_readable"] is True
    assert status["overlay_entries"] == 2
