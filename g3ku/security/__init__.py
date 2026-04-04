from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "BOOTSTRAP_MASTER_KEY_ENV",
    "BootstrapSecurityService",
    "MASTER_KEY_VERSION",
    "SecretOverlayStore",
    "UNLOCK_SCOPE",
    "apply_config_secret_entries",
    "extract_config_secret_entries",
    "get_bootstrap_security_service",
    "strip_config_secret_entries",
]


def __getattr__(name: str) -> Any:
    if name in set(__all__):
        return getattr(import_module("g3ku.security.bootstrap"), name)
    raise AttributeError(name)
