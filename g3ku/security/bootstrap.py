from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken


MASTER_KEY_VERSION = 2
UNLOCK_SCOPE = "global"
DESTROY_CONFIRM_TEXT = "DESTROY ALL SECRET REALMS"
LEGACY_EXPORT_PREFIX = "legacy-secret-export"
MIGRATION_BACKUP_DIR = "security-migration-backups"
REALM_OVERLAY_DIR = "secret-realms"
SCONFIG = "config"
SLLM = "llm_config"
SCRT = "chinaBridge"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _llm_config_root(workspace: Path) -> Path:
    return workspace / ".g3ku" / "llm-config"


def _master_key_path(workspace: Path) -> Path:
    return _llm_config_root(workspace) / "master.key"


def _overlay_root(workspace: Path) -> Path:
    return workspace / ".g3ku" / REALM_OVERLAY_DIR


def _config_path(workspace: Path) -> Path:
    return workspace / ".g3ku" / "config.json"


def _json_load(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _realm_overlay_path(workspace: Path, realm_id: str) -> Path:
    return _overlay_root(workspace) / f"{realm_id}.enc"


def _fernet_from_key(key: str) -> Fernet:
    return Fernet(str(key).encode("utf-8"))


def _derive_password_key(password: str, *, salt: bytes, n: int, r: int, p: int) -> str:
    derived = hashlib.scrypt(
        str(password or "").encode("utf-8"),
        salt=salt,
        n=max(2, int(n)),
        r=max(1, int(r)),
        p=max(1, int(p)),
        dklen=32,
    )
    return base64.urlsafe_b64encode(derived).decode("utf-8")


def _deep_get(payload: dict[str, Any], path: list[str]) -> Any:
    cursor: Any = payload
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return None
        cursor = cursor[key]
    return cursor


def _deep_set(payload: dict[str, Any], path: list[str], value: Any) -> None:
    cursor = payload
    for key in path[:-1]:
        child = cursor.get(key)
        if not isinstance(child, dict):
            child = {}
            cursor[key] = child
        cursor = child
    cursor[path[-1]] = value


def _secret_value_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_secret_value_present(item) for item in value.values())
    if isinstance(value, list):
        return any(_secret_value_present(item) for item in value)
    return value not in (None, False, 0, "", {}, [])


def extract_config_secret_entries(raw_data: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(raw_data) if isinstance(raw_data, dict) else {}
    out: dict[str, Any] = {}

    providers = payload.get("providers")
    if isinstance(providers, dict):
        for provider_name, provider_payload in providers.items():
            if not isinstance(provider_payload, dict):
                continue
            api_key = provider_payload.get("apiKey")
            if _secret_value_present(api_key):
                out[f"{SCONFIG}.providers.{provider_name}.apiKey"] = str(api_key)
            extra_headers = provider_payload.get("extraHeaders")
            if _secret_value_present(extra_headers):
                out[f"{SCONFIG}.providers.{provider_name}.extraHeaders"] = deepcopy(extra_headers)

    tool_secrets = payload.get("toolSecrets")
    if isinstance(tool_secrets, dict):
        for tool_name, tool_payload in tool_secrets.items():
            if _secret_value_present(tool_payload):
                out[f"{SCONFIG}.toolSecrets.{tool_name}"] = deepcopy(tool_payload)

    china_bridge = payload.get("chinaBridge")
    if isinstance(china_bridge, dict):
        control_token = china_bridge.get("controlToken")
        if _secret_value_present(control_token):
            out[f"{SCONFIG}.chinaBridge.controlToken"] = str(control_token)
        channels = china_bridge.get("channels")
        if isinstance(channels, dict):
            secret_fields = {
                "clientSecret",
                "token",
                "secret",
                "corpSecret",
                "appSecret",
                "encodingAesKey",
                "accounts",
            }
            for channel_name, channel_payload in channels.items():
                if not isinstance(channel_payload, dict):
                    continue
                for field_name in secret_fields:
                    value = channel_payload.get(field_name)
                    if _secret_value_present(value):
                        out[f"{SCONFIG}.chinaBridge.channels.{channel_name}.{field_name}"] = deepcopy(value)

    return out


def strip_config_secret_entries(raw_data: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(raw_data) if isinstance(raw_data, dict) else {}

    providers = payload.get("providers")
    if isinstance(providers, dict):
        for provider_payload in providers.values():
            if not isinstance(provider_payload, dict):
                continue
            provider_payload["apiKey"] = ""
            provider_payload["extraHeaders"] = None

    tool_secrets = payload.get("toolSecrets")
    if isinstance(tool_secrets, dict):
        for tool_name in list(tool_secrets.keys()):
            tool_secrets[tool_name] = {}

    china_bridge = payload.get("chinaBridge")
    if isinstance(china_bridge, dict):
        china_bridge["controlToken"] = ""
        channels = china_bridge.get("channels")
        if isinstance(channels, dict):
            for channel_payload in channels.values():
                if not isinstance(channel_payload, dict):
                    continue
                for field_name in (
                    "clientSecret",
                    "token",
                    "secret",
                    "corpSecret",
                    "appSecret",
                    "encodingAesKey",
                ):
                    if field_name in channel_payload:
                        channel_payload[field_name] = ""
                if "accounts" in channel_payload:
                    channel_payload["accounts"] = {}
    return payload


def apply_config_secret_entries(raw_data: dict[str, Any], secret_entries: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(raw_data) if isinstance(raw_data, dict) else {}
    for raw_key, value in dict(secret_entries or {}).items():
        key = str(raw_key or "").strip()
        if not key.startswith(f"{SCONFIG}."):
            continue
        path = key.split(".")[1:]
        if path:
            _deep_set(payload, path, deepcopy(value))
    return payload


@dataclass(slots=True)
class ActiveRealmContext:
    realm_id: str
    display_name: str
    master_key: str


class SecretOverlayStore:
    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()

    def load(self, *, realm_id: str, master_key: str) -> dict[str, Any]:
        path = _realm_overlay_path(self.workspace, realm_id)
        if not path.exists():
            return {}
        try:
            decrypted = _fernet_from_key(master_key).decrypt(path.read_bytes())
            payload = json.loads(decrypted.decode("utf-8"))
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def save(self, *, realm_id: str, master_key: str, payload: dict[str, Any]) -> None:
        path = _realm_overlay_path(self.workspace, realm_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        encrypted = _fernet_from_key(master_key).encrypt(
            json.dumps(payload or {}, ensure_ascii=False, indent=2).encode("utf-8")
        )
        path.write_bytes(encrypted)

    def delete(self, *, realm_id: str) -> None:
        path = _realm_overlay_path(self.workspace, realm_id)
        if path.exists():
            path.unlink()

    def delete_all(self) -> None:
        root = _overlay_root(self.workspace)
        if root.exists():
            shutil.rmtree(root)


class BootstrapSecurityService:
    def __init__(self, workspace: Path | None = None):
        self.workspace = (workspace or Path.cwd()).resolve()
        self._lock = threading.RLock()
        self._overlay_store = SecretOverlayStore(self.workspace)
        self._active: ActiveRealmContext | None = None
        self._overlay_cache: dict[str, Any] = {}

    def status(self) -> dict[str, Any]:
        with self._lock:
            envelope = self._load_master_envelope()
            legacy_detected = self.legacy_detected()
            if self._active is not None:
                return {
                    "mode": "unlocked",
                    "unlock_scope": UNLOCK_SCOPE,
                    "realm_count": len(list(envelope.get("realms") or [])) if isinstance(envelope, dict) else 0,
                    "active_realm_display_name": self._active.display_name,
                    "legacy_detected": legacy_detected,
                }
            if isinstance(envelope, dict) and list(envelope.get("realms") or []):
                return {
                    "mode": "locked",
                    "unlock_scope": UNLOCK_SCOPE,
                    "realm_count": len(list(envelope.get("realms") or [])),
                    "active_realm_display_name": None,
                    "legacy_detected": legacy_detected,
                }
            return {
                "mode": "setup",
                "unlock_scope": UNLOCK_SCOPE,
                "realm_count": 0,
                "active_realm_display_name": None,
                "legacy_detected": legacy_detected,
            }

    def is_unlocked(self) -> bool:
        with self._lock:
            return self._active is not None

    def active_realm(self) -> ActiveRealmContext | None:
        with self._lock:
            return self._active

    def current_overlay(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._overlay_cache)

    def get_overlay_value(self, key: str, default: Any = None) -> Any:
        with self._lock:
            if not self._active:
                return default
            return deepcopy(self._overlay_cache.get(str(key or "").strip(), default))

    def set_overlay_values(self, updates: dict[str, Any]) -> None:
        with self._lock:
            if self._active is None:
                raise ValueError("project is locked")
            for key, value in dict(updates or {}).items():
                normalized = str(key or "").strip()
                if not normalized:
                    continue
                if value in (None, "", {}, []):
                    self._overlay_cache.pop(normalized, None)
                else:
                    self._overlay_cache[normalized] = deepcopy(value)
            self._persist_active_overlay()

    def delete_overlay_keys(self, keys: list[str]) -> None:
        with self._lock:
            if self._active is None:
                raise ValueError("project is locked")
            for key in list(keys or []):
                self._overlay_cache.pop(str(key or "").strip(), None)
            self._persist_active_overlay()

    def delete_overlay_prefix_all_realms(self, prefix: str) -> None:
        normalized = str(prefix or "").strip()
        if not normalized:
            return
        with self._lock:
            envelope = self._load_master_envelope()
            for realm in list((envelope or {}).get("realms") or []):
                if not isinstance(realm, dict):
                    continue
                realm_id = str(realm.get("realm_id") or "").strip()
                try:
                    realm_key = self._unwrap_realm_master_key(realm=realm, password=None, allow_active=True)
                except Exception:
                    continue
                if not realm_id or not realm_key:
                    continue
                payload = self._overlay_store.load(realm_id=realm_id, master_key=realm_key)
                next_payload = {
                    key: value
                    for key, value in payload.items()
                    if not str(key or "").strip().startswith(normalized)
                }
                self._overlay_store.save(realm_id=realm_id, master_key=realm_key, payload=next_payload)
                if self._active is not None and self._active.realm_id == realm_id:
                    self._overlay_cache = next_payload

    def setup_initial_realm(
        self,
        *,
        password: str,
        display_name: str,
        confirm_legacy_reset: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            envelope = self._load_master_envelope()
            if isinstance(envelope, dict) and list(envelope.get("realms") or []):
                raise ValueError("secret realms already exist")
            if self.legacy_detected():
                if not confirm_legacy_reset:
                    raise ValueError("legacy secrets detected; explicit reset confirmation is required")
                self._migrate_legacy_state()
            envelope = {
                "version": MASTER_KEY_VERSION,
                "unlock_scope": UNLOCK_SCOPE,
                "realms": [],
            }
            realm = self._create_realm_entry(password=password, display_name=display_name)
            envelope["realms"].append(realm)
            self._write_master_envelope(envelope)
            self._activate_realm(realm=realm, password=password)
            return self.status()

    def unlock(self, *, password: str) -> dict[str, Any]:
        with self._lock:
            envelope = self._require_master_envelope()
            for realm in list(envelope.get("realms") or []):
                if not isinstance(realm, dict):
                    continue
                try:
                    master_key = self._unwrap_realm_master_key(realm=realm, password=password)
                except Exception:
                    continue
                self._active = ActiveRealmContext(
                    realm_id=str(realm.get("realm_id") or "").strip(),
                    display_name=str(realm.get("display_name") or "").strip() or "Secret Realm",
                    master_key=master_key,
                )
                self._overlay_cache = self._overlay_store.load(
                    realm_id=self._active.realm_id,
                    master_key=self._active.master_key,
                )
                return self.status()
            raise ValueError("invalid password")

    def lock(self) -> dict[str, Any]:
        with self._lock:
            self._active = None
            self._overlay_cache = {}
            return self.status()

    def rename_current_realm(self, *, display_name: str) -> dict[str, Any]:
        with self._lock:
            active = self._require_active_realm()
            envelope = self._require_master_envelope()
            changed = False
            for realm in list(envelope.get("realms") or []):
                if str(realm.get("realm_id") or "").strip() != active.realm_id:
                    continue
                realm["display_name"] = str(display_name or "").strip() or active.display_name
                realm["updated_at"] = _now_iso()
                active.display_name = realm["display_name"]
                changed = True
                break
            if not changed:
                raise ValueError("active realm not found")
            self._write_master_envelope(envelope)
            return self.status()

    def create_realm(self, *, password: str, display_name: str) -> dict[str, Any]:
        with self._lock:
            envelope = self._require_master_envelope()
            realm = self._create_realm_entry(password=password, display_name=display_name)
            envelope.setdefault("realms", []).append(realm)
            self._write_master_envelope(envelope)
            self._overlay_store.save(
                realm_id=str(realm.get("realm_id") or ""),
                master_key=self._unwrap_realm_master_key(realm=realm, password=password),
                payload={},
            )
            return self.status()

    def destroy_all_secrets(self, *, confirm_text: str) -> dict[str, Any]:
        if str(confirm_text or "").strip() != DESTROY_CONFIRM_TEXT:
            raise ValueError("confirmation text mismatch")
        with self._lock:
            self._active = None
            self._overlay_cache = {}
            self._overlay_store.delete_all()
            master_key_path = _master_key_path(self.workspace)
            if master_key_path.exists():
                master_key_path.unlink()
            return self.status()

    def legacy_detected(self) -> bool:
        with self._lock:
            return self._legacy_master_key_present() or self._legacy_config_secrets_present() or self._legacy_llm_records_present()

    def export_legacy_state(self) -> dict[str, Any]:
        with self._lock:
            return self._migrate_legacy_state(dry_run=True)

    def _persist_active_overlay(self) -> None:
        active = self._require_active_realm()
        self._overlay_store.save(
            realm_id=active.realm_id,
            master_key=active.master_key,
            payload=self._overlay_cache,
        )

    def _create_realm_entry(self, *, password: str, display_name: str) -> dict[str, Any]:
        clean_password = str(password or "")
        if len(clean_password) < 1:
            raise ValueError("password is required")
        salt = os.urandom(16)
        realm_master_key = Fernet.generate_key().decode("utf-8")
        kdf = {"name": "scrypt", "n": 16384, "r": 8, "p": 1}
        derived_key = _derive_password_key(clean_password, salt=salt, n=kdf["n"], r=kdf["r"], p=kdf["p"])
        wrapped = _fernet_from_key(derived_key).encrypt(realm_master_key.encode("utf-8"))
        now = _now_iso()
        return {
            "realm_id": uuid4().hex,
            "display_name": str(display_name or "").strip() or "Secret Realm",
            "salt_b64": base64.b64encode(salt).decode("ascii"),
            "kdf": kdf,
            "wrapped_master_key_b64": base64.b64encode(wrapped).decode("ascii"),
            "created_at": now,
            "updated_at": now,
        }

    def _activate_realm(self, *, realm: dict[str, Any], password: str) -> None:
        master_key = self._unwrap_realm_master_key(realm=realm, password=password)
        self._active = ActiveRealmContext(
            realm_id=str(realm.get("realm_id") or "").strip(),
            display_name=str(realm.get("display_name") or "").strip() or "Secret Realm",
            master_key=master_key,
        )
        self._overlay_cache = self._overlay_store.load(
            realm_id=self._active.realm_id,
            master_key=self._active.master_key,
        )
        self._persist_active_overlay()

    def _unwrap_realm_master_key(
        self,
        *,
        realm: dict[str, Any],
        password: str | None,
        allow_active: bool = False,
    ) -> str:
        realm_id = str(realm.get("realm_id") or "").strip()
        if allow_active and self._active is not None and self._active.realm_id == realm_id:
            return self._active.master_key
        if password is None:
            raise ValueError("password is required")
        kdf = realm.get("kdf") if isinstance(realm.get("kdf"), dict) else {}
        salt_b64 = str(realm.get("salt_b64") or "").strip()
        wrapped_b64 = str(realm.get("wrapped_master_key_b64") or "").strip()
        salt = base64.b64decode(salt_b64.encode("ascii"))
        wrapped = base64.b64decode(wrapped_b64.encode("ascii"))
        derived_key = _derive_password_key(
            str(password or ""),
            salt=salt,
            n=int(kdf.get("n") or 16384),
            r=int(kdf.get("r") or 8),
            p=int(kdf.get("p") or 1),
        )
        try:
            return _fernet_from_key(derived_key).decrypt(wrapped).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("invalid password") from exc

    def _load_master_envelope(self) -> dict[str, Any] | None:
        path = _master_key_path(self.workspace)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version") or 0) != MASTER_KEY_VERSION:
            return None
        if str(payload.get("unlock_scope") or "").strip() != UNLOCK_SCOPE:
            return None
        realms = payload.get("realms")
        if not isinstance(realms, list):
            return None
        return payload

    def _write_master_envelope(self, payload: dict[str, Any]) -> None:
        _json_dump(_master_key_path(self.workspace), payload)

    def _require_master_envelope(self) -> dict[str, Any]:
        payload = self._load_master_envelope()
        if payload is None:
            raise ValueError("secret realms are not configured")
        return payload

    def _require_active_realm(self) -> ActiveRealmContext:
        if self._active is None:
            raise ValueError("project is locked")
        return self._active

    def _legacy_master_key_present(self) -> bool:
        path = _master_key_path(self.workspace)
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return bool(path.read_text(encoding="utf-8").strip())
        return not (isinstance(payload, dict) and int(payload.get("version") or 0) == MASTER_KEY_VERSION)

    def _legacy_config_secrets_present(self) -> bool:
        return bool(extract_config_secret_entries(_json_load(_config_path(self.workspace), default={})))

    def _legacy_llm_records_present(self) -> bool:
        records_root = _llm_config_root(self.workspace) / "records"
        if not records_root.exists():
            return False
        return any(path.suffix.lower() == ".enc" for path in records_root.glob("*.enc"))

    def _migrate_legacy_state(self, *, dry_run: bool = False) -> dict[str, Any]:
        from g3ku.llm_config.models import NormalizedProviderConfig
        from g3ku.llm_config.repositories import EncryptedConfigRepository
        from g3ku.llm_config.secret_store import EncryptedFileSecretStore

        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup_root = self.workspace / ".g3ku" / MIGRATION_BACKUP_DIR / timestamp
        export_path = self.workspace / ".g3ku" / f"{LEGACY_EXPORT_PREFIX}-{timestamp}.json"
        config_path = _config_path(self.workspace)
        raw_config = _json_load(config_path, default={})
        config_secrets = extract_config_secret_entries(raw_config)

        llm_root = _llm_config_root(self.workspace)
        legacy_key_text = ""
        master_path = _master_key_path(self.workspace)
        if master_path.exists():
            try:
                json.loads(master_path.read_text(encoding="utf-8"))
            except Exception:
                legacy_key_text = master_path.read_text(encoding="utf-8").strip()

        llm_secret_export: dict[str, Any] = {}
        llm_records: list[NormalizedProviderConfig] = []
        if legacy_key_text:
            repo = EncryptedConfigRepository(
                llm_root,
                EncryptedFileSecretStore(legacy_key_text),
            )
            for summary in repo.list_summaries():
                try:
                    record = repo.get(summary.config_id)
                except Exception:
                    continue
                llm_records.append(record)
                if _secret_value_present(record.auth):
                    llm_secret_export[f"{SLLM}.{record.config_id}.auth"] = deepcopy(record.auth)
                if _secret_value_present(record.headers):
                    llm_secret_export[f"{SLLM}.{record.config_id}.headers"] = deepcopy(record.headers)

        export_payload = {
            "created_at": _now_iso(),
            "workspace": str(self.workspace),
            "config_secrets": config_secrets,
            "llm_config_secrets": llm_secret_export,
        }

        if dry_run:
            return {
                "legacy_detected": self.legacy_detected(),
                "export_path": str(export_path),
                "backup_root": str(backup_root),
                "config_secret_count": len(config_secrets),
                "llm_secret_count": len(llm_secret_export),
            }

        backup_root.mkdir(parents=True, exist_ok=True)
        _json_dump(export_path, export_payload)

        if config_path.exists():
            shutil.copy2(config_path, backup_root / "config.json")
        if llm_root.exists():
            shutil.copytree(llm_root, backup_root / "llm-config", dirs_exist_ok=True)

        sanitized_config = strip_config_secret_entries(raw_config)
        _json_dump(config_path, sanitized_config)

        llm_root.mkdir(parents=True, exist_ok=True)
        (llm_root / "records").mkdir(parents=True, exist_ok=True)
        for path in (llm_root / "records").glob("*"):
            if path.is_file():
                path.unlink()
        from g3ku.llm_config.repositories import EncryptedConfigRepository as PlainRepo

        plain_repo = PlainRepo(llm_root, None)
        index_entries = []
        for record in llm_records:
            sanitized_record = record.model_copy(
                update={
                    "auth": {"type": str(record.auth.get("type") or record.auth_mode.value), "api_key": ""},
                    "headers": {},
                }
            )
            summary = plain_repo.save(sanitized_record, last_probe_status=None)
            index_entries.append(summary)
        if hasattr(plain_repo, "_write_index"):
            plain_repo._write_index(index_entries)
        if master_path.exists():
            master_path.unlink()
        return {
            "legacy_detected": True,
            "export_path": str(export_path),
            "backup_root": str(backup_root),
            "config_secret_count": len(config_secrets),
            "llm_secret_count": len(llm_secret_export),
        }


_SERVICES: dict[str, BootstrapSecurityService] = {}
_SERVICES_LOCK = threading.RLock()


def get_bootstrap_security_service(workspace: Path | None = None) -> BootstrapSecurityService:
    root = (workspace or Path.cwd()).resolve()
    key = str(root).lower()
    with _SERVICES_LOCK:
        service = _SERVICES.get(key)
        if service is None:
            service = BootstrapSecurityService(root)
            _SERVICES[key] = service
        return service


__all__ = [
    "ActiveRealmContext",
    "BootstrapSecurityService",
    "DESTROY_CONFIRM_TEXT",
    "MASTER_KEY_VERSION",
    "SecretOverlayStore",
    "UNLOCK_SCOPE",
    "apply_config_secret_entries",
    "extract_config_secret_entries",
    "get_bootstrap_security_service",
    "strip_config_secret_entries",
]
