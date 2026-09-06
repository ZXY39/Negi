"""Regression tests for the secret overlay destruction guard.

An overlay that cannot be decrypted with the active master key must never be
overwritten: a wrong-key activation (e.g. an unrelated instance auto-unlocking
against this workspace via G3KU_BOOTSTRAP_MASTER_KEY) used to persist an
empty overlay and destroy the real apikey secrets stored there.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from g3ku.security.bootstrap import BootstrapSecurityService


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


def _overlay_path(workspace):
    return workspace / ".g3ku" / "secret-realms" / "default.enc"


def test_wrong_master_key_activation_cannot_destroy_overlay(workspace):
    service = BootstrapSecurityService(workspace)
    service.setup_initial_realm(password="owner-password")
    service.set_overlay_values({"config.providers.openai.apiKey": "precious-key"})
    service.lock()

    blob_before = _overlay_path(workspace).read_bytes()

    # An unrelated instance activates with a key that cannot decrypt the
    # existing overlay: it must see an empty view AND leave the file intact.
    intruder = BootstrapSecurityService(workspace)
    status = intruder.activate_with_master_key(master_key=Fernet.generate_key().decode("utf-8"))
    assert status["mode"] == "unlocked"
    assert intruder.current_overlay() == {}
    assert _overlay_path(workspace).read_bytes() == blob_before

    # Mutations under the wrong key must refuse instead of destroying.
    with pytest.raises(ValueError, match="undecryptable"):
        intruder.set_overlay_values({"config.providers.openai.apiKey": ""})
    assert _overlay_path(workspace).read_bytes() == blob_before

    # The rightful owner still recovers everything.
    service.unlock(password="owner-password")
    assert service.current_overlay() == {"config.providers.openai.apiKey": "precious-key"}


def test_correct_master_key_activation_still_works(workspace):
    service = BootstrapSecurityService(workspace)
    service.setup_initial_realm(password="owner-password")
    service.set_overlay_values({"config.example": "value-1"})
    master_key = service.active_master_key()
    assert master_key

    second = BootstrapSecurityService(workspace)
    status = second.activate_with_master_key(master_key=master_key)
    assert status["mode"] == "unlocked"
    assert second.current_overlay() == {"config.example": "value-1"}
    # Verified activation may persist (round-trip keeps content).
    second.set_overlay_values({"config.example": "value-2"})
    third = BootstrapSecurityService(workspace)
    third.activate_with_master_key(master_key=master_key)
    assert third.current_overlay() == {"config.example": "value-2"}


def test_unlock_with_password_unaffected_by_guard(workspace):
    service = BootstrapSecurityService(workspace)
    service.setup_initial_realm(password="owner-password")
    service.set_overlay_values({"config.k": "v"})
    service.lock()

    with pytest.raises(ValueError, match="invalid password"):
        service.unlock(password="wrong")

    service.unlock(password="owner-password")
    assert service.current_overlay() == {"config.k": "v"}
