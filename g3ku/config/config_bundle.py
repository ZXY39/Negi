"""口令加密的配置包：把一台部署的配置面导出成单文件，在另一台上还原。

包内容只覆盖配置面（结构配置、provider 记录、密钥覆盖层、资源开关、治理库），
不含任务、会话与取证数据。导出要求进程已解锁，因为包里必须带上主密钥本身。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.fernet import InvalidToken

from g3ku.security import (
    PASSWORD_KDF,
    derive_password_key,
    fernet_from_key,
    get_bootstrap_security_service,
)

BUNDLE_KIND = "g3ku-config-bundle"
BUNDLE_VERSION = 1
BUNDLE_EXTENSION = ".g3kucb"
BUNDLE_OUTPUT_DIR = ".g3ku/config-bundles"
IMPORT_BACKUP_DIR = ".g3ku/config-bundle-imports"

CONFIG_FILENAME = ".g3ku/config.json"
RESOURCES_STATE_PATH = ".g3ku/resources.state.json"
GOVERNANCE_STORE_PATH = ".g3ku/main-runtime/governance.sqlite3"
SECRET_REALMS_DIR = ".g3ku/secret-realms"
LLM_CONFIG_DIR = ".g3ku/llm-config"

# 信封锁的是源部署的登录口令，auto-unlock.key 是 bearer credential：两者都不进包，
# 主密钥改由包内 payload 携带，导入时按包口令重新包裹。
EXCLUDED_NAMES = {"master.key", "auto-unlock.key"}
SQLITE_SUFFIXES = {".sqlite3", ".db"}
COMPANION_SUFFIXES = {".wal", ".shm"}

CONFIG_BUNDLE_ROOT = ".g3ku"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _key_for(password: str, salt: bytes) -> str:
    return derive_password_key(
        password,
        salt=salt,
        n=int(PASSWORD_KDF["n"]),
        r=int(PASSWORD_KDF["r"]),
        p=int(PASSWORD_KDF["p"]),
    )


def _raw_config(workspace: Path) -> dict[str, Any]:
    try:
        data = json.loads((workspace / CONFIG_FILENAME).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def _path_field(section: dict[str, Any], *names: str, default: str) -> str:
    # 落盘配置走 camelCase 别名，直读 JSON 时两种拼法都要认。
    for name in names:
        text = str(section.get(name) or "").strip()
        if text:
            return text
    return default


def bundle_paths(workspace: Path) -> list[str]:
    """配置面包裹哪些相对路径：默认值取自 schema，配置改写后跟随配置。"""
    raw = _raw_config(workspace)
    candidates = [
        CONFIG_FILENAME,
        _path_field(_section(raw, "resources"), "state_path", "statePath", default=RESOURCES_STATE_PATH),
        _path_field(
            _section(raw, "main_runtime"),
            "governance_store_path",
            "governanceStorePath",
            default=GOVERNANCE_STORE_PATH,
        ),
        SECRET_REALMS_DIR,
        LLM_CONFIG_DIR,
    ]
    return sorted(dict.fromkeys(candidates))


def _relative_to(workspace: Path, path: Path) -> str:
    return path.resolve().relative_to(workspace.resolve()).as_posix()


def _iter_source_files(workspace: Path) -> list[Path]:
    files: list[Path] = []
    for rel in bundle_paths(workspace):
        root = workspace / rel
        if root.is_dir():
            files.extend(
                child
                for child in sorted(root.rglob("*"))
                if child.is_file() and child.name not in EXCLUDED_NAMES
                and child.suffix not in COMPANION_SUFFIXES
            )
        elif root.is_file():
            files.append(root)
    return files


def _snapshot_sqlite(source: Path) -> bytes:
    # sqlite3 的 with conn 只提交事务、不关连接，句柄留着会让 Windows 删不掉临时目录。
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / source.name
        with (
            closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as read_conn,
            closing(sqlite3.connect(target)) as write_conn,
        ):
            read_conn.backup(write_conn)
        return target.read_bytes()


def _restore_sqlite(blob: bytes, target: Path) -> None:
    """写回正被其他连接持有的库文件要用在线 backup，替换文件会撞上残留 -wal。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / target.name
        source.write_bytes(blob)
        with (
            closing(sqlite3.connect(source)) as read_conn,
            closing(sqlite3.connect(target)) as write_conn,
        ):
            read_conn.backup(write_conn)


def _collect_entries(workspace: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for path in _iter_source_files(workspace):
        rel = _relative_to(workspace, path)
        blob = _snapshot_sqlite(path) if path.suffix in SQLITE_SUFFIXES else path.read_bytes()
        entries[rel] = base64.b64encode(blob).decode("ascii")
    return entries


def _validate_password(password: str) -> str:
    text = str(password or "")
    if not text:
        raise ValueError("password is required")
    return text


def export_bundle(workspace: Path | None = None, *, password: str) -> dict[str, Any]:
    root = (workspace or Path.cwd()).resolve()
    service = get_bootstrap_security_service(root)
    master_key = service.active_master_key()
    if not master_key:
        raise ValueError("project is locked")
    clean_password = _validate_password(password)

    stamp = _stamp()
    created_at = _now_iso()
    entries = _collect_entries(root)
    payload = {
        "created_at": created_at,
        "workspace_label": root.name,
        "master_key": master_key,
        "entries": entries,
    }
    salt = os.urandom(16)
    token = fernet_from_key(_key_for(clean_password, salt)).encrypt(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    envelope = {
        "kind": BUNDLE_KIND,
        "version": BUNDLE_VERSION,
        "created_at": created_at,
        "workspace_label": payload["workspace_label"],
        "kdf": dict(PASSWORD_KDF),
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "payload_b64": base64.b64encode(token).decode("ascii"),
    }

    output_dir = root / BUNDLE_OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{BUNDLE_KIND}-{stamp}{BUNDLE_EXTENSION}"
    path = output_dir / filename
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(path, 0o600)

    return {
        "path": str(path),
        "filename": filename,
        "created_at": created_at,
        "workspace_label": payload["workspace_label"],
        "entries": sorted(entries),
        "entry_count": len(entries),
        "bytes": path.stat().st_size,
    }


def _read_envelope(archive_path: Path) -> dict[str, Any]:
    try:
        envelope = json.loads(Path(archive_path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("not a config bundle file") from exc
    if not isinstance(envelope, dict) or envelope.get("kind") != BUNDLE_KIND:
        raise ValueError("not a config bundle file")
    if int(envelope.get("version") or 0) != BUNDLE_VERSION:
        raise ValueError("unsupported config bundle version")
    return envelope


def _decrypt_payload(envelope: dict[str, Any], password: str) -> dict[str, Any]:
    salt_b64 = str(envelope.get("salt_b64") or "").strip()
    payload_b64 = str(envelope.get("payload_b64") or "").strip()
    if not salt_b64 or not payload_b64:
        raise ValueError("invalid config bundle envelope")
    try:
        decrypted = fernet_from_key(_key_for(password, base64.b64decode(salt_b64))).decrypt(
            base64.b64decode(payload_b64)
        )
        payload = json.loads(decrypted.decode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("invalid password") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid config bundle payload")
    return payload


def _safe_relative_path(rel: str) -> PurePosixPath:
    text = str(rel or "").replace("\\", "/").strip()
    parts = PurePosixPath(text).parts
    if not parts or parts[0] != CONFIG_BUNDLE_ROOT:
        raise ValueError(f"bundle entry outside .g3ku rejected: {text}")
    if text.startswith("/") or ".." in parts or ":" in parts[0]:
        raise ValueError(f"unsafe bundle path rejected: {text}")
    return PurePosixPath(*parts)


def _preflight(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    master_key = str(payload.get("master_key") or "").strip()
    if not master_key:
        raise ValueError("bundle carries no master key")
    entries = payload.get("entries")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("bundle carries no config files")
    targets = {str(key): _safe_relative_path(str(key)) for key in entries}
    overlay = entries.get(f"{SECRET_REALMS_DIR}/default.enc")
    if overlay:
        # 先证伪：钥匙打不开覆盖层就整包拒绝，避免装出一个只读空视图的部署。
        try:
            fernet_from_key(master_key).decrypt(base64.b64decode(str(overlay)))
        except InvalidToken as exc:
            raise ValueError("bundle master key cannot decrypt its secret overlay") from exc
    return master_key, targets


def _backup_targets(workspace: Path, targets: dict[str, PurePosixPath], backup_root: Path) -> list[str]:
    backed_up: list[str] = []
    for rel, pure in targets.items():
        source = workspace.joinpath(*pure.parts)
        if not source.exists():
            continue
        destination = backup_root / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(source, destination)
        backed_up.append(rel)
    return backed_up


def _restore_backup(workspace: Path, backup_root: Path, backed_up: list[str]) -> None:
    for rel in backed_up:
        source = backup_root / rel
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)


def _write_entries(workspace: Path, entries: dict[str, str], targets: dict[str, PurePosixPath]) -> None:
    for rel, pure in targets.items():
        blob = base64.b64decode(str(entries[rel]))
        target = workspace.joinpath(*pure.parts)
        if PurePosixPath(rel).suffix in SQLITE_SUFFIXES:
            _restore_sqlite(blob, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            os.chmod(target, 0o600)


def import_bundle(
    workspace: Path | None = None,
    *,
    archive_path: Path | str,
    password: str,
) -> dict[str, Any]:
    root = (workspace or Path.cwd()).resolve()
    payload = _decrypt_payload(_read_envelope(Path(archive_path)), _validate_password(password))
    master_key, targets = _preflight(payload)
    entries = payload["entries"]

    backup_root = root / IMPORT_BACKUP_DIR / _stamp()
    backup_root.mkdir(parents=True, exist_ok=True)
    backed_up = _backup_targets(root, targets, backup_root)
    try:
        _write_entries(root, entries, targets)
        status = get_bootstrap_security_service(root).install_master_key(
            master_key=master_key,
            password=str(password),
        )
    except Exception:
        _restore_backup(root, backup_root, backed_up)
        raise
    return {
        "entries": sorted(targets),
        "entry_count": len(targets),
        "backup_dir": str(backup_root),
        "created_at": str(payload.get("created_at") or ""),
        "workspace_label": str(payload.get("workspace_label") or ""),
        "status": status,
    }


__all__ = [
    "BUNDLE_EXTENSION",
    "BUNDLE_KIND",
    "IMPORT_BACKUP_DIR",
    "bundle_paths",
    "export_bundle",
    "import_bundle",
]
