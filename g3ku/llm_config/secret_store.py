from __future__ import annotations

import os
from typing import Protocol

from cryptography.fernet import Fernet

from .exceptions import MissingMasterKeyError


class SecretStore(Protocol):
    def encrypt(self, plaintext: bytes) -> bytes: ...

    def decrypt(self, ciphertext: bytes) -> bytes: ...


class EncryptedFileSecretStore:
    def __init__(self, key: str | bytes | None = None, *, env_var: str = "LLM_CONFIG_MASTER_KEY"):
        resolved_key = key or os.getenv(env_var)
        if not resolved_key:
            raise MissingMasterKeyError(
                f"Missing master key. Provide it directly or set {env_var}."
            )
        if isinstance(resolved_key, str):
            resolved_key = resolved_key.encode("utf-8")
        self._fernet = Fernet(resolved_key)

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("utf-8")

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._fernet.decrypt(ciphertext)

