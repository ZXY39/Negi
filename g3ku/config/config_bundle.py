"""口令加密的配置包：把一台部署的配置面、记忆面、人设面与表情包面导出成单文件，在另一台上还原。

配置面（结构配置、provider 记录、密钥覆盖层、资源开关、治理库）、记忆面（记忆库、
笔记正文、常驻镜像）、人设面（人设 note 与 `my_image/` 照片）和表情包面（`stickers/`
下的包目录、manifest 与注册表）是四颗独立勾选：没勾的一面既不进包也不落盘，勾上的面
整体替换、不做合并。记忆面不夹带人设 note，人设只随人设面走。不含任务、会话与取证数据。
导出要求进程已解锁，因为带配置面时包里必须有主密钥本身。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from cryptography.fernet import InvalidToken

from g3ku import __version__
from g3ku.agent.markdown_memory import note_file_name
from g3ku.agent.persona import (
    PERSONA_NOTE_REF,
    PERSONA_PHOTO_DIR_NAME,
    PERSONA_PHOTO_SUFFIXES,
)
from g3ku.resources.tool_settings import MemoryRuntimeSettings, load_tool_settings_from_manifest
from g3ku.security import (
    PASSWORD_KDF,
    derive_password_key,
    fernet_from_key,
    get_bootstrap_security_service,
    unwrap_master_key,
)
from g3ku.stickers.admin import admin_segment_ok
from g3ku.stickers.catalog import (
    IMAGE_SUFFIXES,
    MANIFEST_NAME,
    STICKERS_DIRNAME,
    is_builtin_pack,
)
from g3ku.stickers.registry import REGISTRY_NAME

BUNDLE_KIND = "g3ku-config-bundle"
BUNDLE_VERSION = 5
# v2 只有配置面，v3 增加记忆面，v4 增加人设面；旧文件都继续认，不能因为我们多加了面就失效。
CONFIG_ONLY_BUNDLE_VERSION = 2
MEMORY_BUNDLE_VERSION = 3
PERSONA_BUNDLE_VERSION = 4
STICKERS_BUNDLE_VERSION = 5
SUPPORTED_BUNDLE_VERSIONS = (
    CONFIG_ONLY_BUNDLE_VERSION,
    MEMORY_BUNDLE_VERSION,
    PERSONA_BUNDLE_VERSION,
    STICKERS_BUNDLE_VERSION,
)
BUNDLE_EXTENSION = ".g3kucb"
BUNDLE_OUTPUT_DIR = ".g3ku/config-bundles"
IMPORT_BACKUP_DIR = ".g3ku/config-bundle-imports"
KEY_SOURCE_PROJECT_PASSWORD = "project_password"
KEY_SOURCE_BUNDLE_PASSWORD = "bundle_password"

SCOPE_CONFIG = "config"
SCOPE_MEMORY = "memory"
SCOPE_PERSONA = "persona"
SCOPE_STICKERS = "stickers"
BUNDLE_SCOPES = (SCOPE_CONFIG, SCOPE_MEMORY, SCOPE_PERSONA, SCOPE_STICKERS)
V4_BUNDLE_SCOPES = (SCOPE_CONFIG, SCOPE_MEMORY, SCOPE_PERSONA)
V3_BUNDLE_SCOPES = (SCOPE_CONFIG, SCOPE_MEMORY)

CONFIG_FILENAME = ".g3ku/config.json"
RESOURCES_STATE_PATH = ".g3ku/resources.state.json"
GOVERNANCE_STORE_PATH = ".g3ku/main-runtime/governance.sqlite3"
SECRET_REALMS_DIR = ".g3ku/secret-realms"
LLM_CONFIG_DIR = ".g3ku/llm-config"

# 记忆面锚在运行时自己的 workspace 上，落点按 memory_runtime 清单跟随配置改写；
# 包内只用下面这套逻辑名，导入时再映射回目标机自己算出来的路径。
MEMORY_DIR_ROOT = "memory"
MEMORY_MIRROR_KEY = "memory/MEMORY.md"
MEMORY_STATE_DB_KEY = "memory/memory_state.sqlite3"
MEMORY_NOTES_KEY = "memory/notes"
MEMORY_INDEX_KEY = "memory/index"
# 群聊记忆：每个群一个独立记忆根，包内逻辑键保持 `memory/groups/<scope>/...`。
GROUP_MEMORY_ROOT = f"{MEMORY_DIR_ROOT}/groups"
GROUP_MEMORY_MIRROR_NAME = "MEMORY.md"
GROUP_MEMORY_STATE_DB_NAME = "memory_state.sqlite3"
GROUP_MEMORY_NOTE_DIRS = ("notes", "index")
MEMORY_TOOL_NAME = "memory_runtime"
MEMORY_NOTE_SUFFIXES = {".md"}

# 人设面用独立逻辑根：人设 note 与记忆正文同目录，靠前缀把它从记忆面里摘出来。
PERSONA_DIR_ROOT = "persona"
PERSONA_NOTE_KEY = f"{PERSONA_DIR_ROOT}/{note_file_name(PERSONA_NOTE_REF)}"
PERSONA_PHOTOS_KEY = f"{PERSONA_DIR_ROOT}/{PERSONA_PHOTO_DIR_NAME}"

# 信封锁的是源部署的登录口令，auto-unlock.key 是 bearer credential：两者都不进包，
# 主密钥改由包内 payload 携带，导入时按包口令重新包裹。
EXCLUDED_NAMES = {"master.key", "auto-unlock.key"}
SQLITE_SUFFIXES = {".sqlite3", ".db"}
COMPANION_SUFFIXES = {".wal", ".shm"}

# 表情包面锚在 workspace 根，与 catalog.stickers_root() 同一落点；包内逻辑名就是
# `<pack>/<file>` 这层相对路径，导入时按本机 workspace 重新拼。
STICKERS_DIR_ROOT = STICKERS_DIRNAME
STICKER_PACK_FILES = frozenset({MANIFEST_NAME, REGISTRY_NAME})

CONFIG_BUNDLE_ROOT = ".g3ku"
BUNDLE_ROOTS = (CONFIG_BUNDLE_ROOT, MEMORY_DIR_ROOT, PERSONA_DIR_ROOT, STICKERS_DIR_ROOT)


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


def _workspace_anchor(root: Path) -> Path:
    """记忆与工具清单一律锚在 `agents.defaults.workspace`，不是包根的 cwd。

    `g3ku/web/launcher.py` 启动时 chdir 到项目根，默认情况下两者重合；操作员改过这一项
    时只有这个锚点能指到记忆真正在的目录，否则会导出一份空记忆而不是报错。
    """
    raw = str(_section(_section(_raw_config(root), "agents"), "defaults").get("workspace") or "").strip()
    if not raw:
        return root
    path = Path(raw).expanduser()
    return Path(os.path.normpath(str(path if path.is_absolute() else root / path)))


def _memory_landing_paths(root: Path) -> dict[str, Path]:
    """记忆面在这台机器上的四个落点，键就是包内逻辑名。"""
    anchor = _workspace_anchor(root)
    memory_file = MEMORY_MIRROR_KEY
    notes_dir = MEMORY_NOTES_KEY
    try:
        # 清单缺失或读不动都回默认值：导入要能在还没走完解锁/种子流程的新设备上跑。
        settings = load_tool_settings_from_manifest(anchor, MEMORY_TOOL_NAME, MemoryRuntimeSettings)
        memory_file = str(settings.document.memory_file or "").strip() or memory_file
        notes_dir = str(settings.document.notes_dir or "").strip() or notes_dir
    except Exception:
        pass
    return {
        MEMORY_MIRROR_KEY: anchor / memory_file,
        MEMORY_STATE_DB_KEY: anchor / MEMORY_STATE_DB_KEY,
        MEMORY_NOTES_KEY: anchor / notes_dir,
        MEMORY_INDEX_KEY: anchor / MEMORY_INDEX_KEY,
    }


def _persona_landing_paths(
    root: Path, memory_land: dict[str, Path] | None = None
) -> dict[str, Path]:
    """人设面在这台机器上的两个落点：保留 note 跟随记忆清单，照片固定在 workspace 根。"""
    land = memory_land if memory_land is not None else _memory_landing_paths(root)
    return {
        PERSONA_NOTE_KEY: land[MEMORY_NOTES_KEY] / note_file_name(PERSONA_NOTE_REF),
        PERSONA_PHOTOS_KEY: _workspace_anchor(root) / PERSONA_PHOTO_DIR_NAME,
    }


def _stickers_landing_paths(root: Path) -> dict[str, Path]:
    """表情包面在这台机器上的落点：`<workspace>/stickers` 整棵树。"""
    return {STICKERS_DIR_ROOT: _workspace_anchor(root) / STICKERS_DIR_ROOT}


def _entry_scope(rel: str) -> str:
    text = str(rel or "").strip().replace("\\", "/")
    if text.startswith(f"{CONFIG_BUNDLE_ROOT}/"):
        return SCOPE_CONFIG
    if text.startswith(f"{MEMORY_DIR_ROOT}/"):
        return SCOPE_MEMORY
    if text.startswith(f"{PERSONA_DIR_ROOT}/"):
        return SCOPE_PERSONA
    if text.startswith(f"{STICKERS_DIR_ROOT}/"):
        return SCOPE_STICKERS
    return ""


def _validate_scopes(
    scopes: Sequence[str] | None, *, allowed: Sequence[str] = BUNDLE_SCOPES
) -> tuple[str, ...]:
    cleaned = {str(scope or "").strip().lower() for scope in (scopes or ())}
    cleaned.discard("")
    unknown = sorted(cleaned - set(allowed))
    if unknown:
        raise ValueError(f"unknown bundle scope: {unknown[0]}")
    if not cleaned:
        raise ValueError("bundle_scope_empty")
    return tuple(scope for scope in allowed if scope in cleaned)


def _envelope_scopes(envelope: dict[str, Any]) -> tuple[str, ...]:
    version = int(envelope.get("version") or 0)
    if version < MEMORY_BUNDLE_VERSION:
        return (SCOPE_CONFIG,)
    if version < PERSONA_BUNDLE_VERSION:
        allowed = V3_BUNDLE_SCOPES
    elif version < STICKERS_BUNDLE_VERSION:
        allowed = V4_BUNDLE_SCOPES
    else:
        allowed = BUNDLE_SCOPES
    return _validate_scopes(envelope.get("scopes"), allowed=allowed)


def _bundle_version_for_scopes(scopes: Sequence[str]) -> int:
    # 版本闸门逐面抬高：只带配置/记忆的包保持 v3，带人设是 v4，带表情包才需要 v5。
    if SCOPE_STICKERS in scopes:
        return STICKERS_BUNDLE_VERSION
    if SCOPE_PERSONA in scopes:
        return PERSONA_BUNDLE_VERSION
    return MEMORY_BUNDLE_VERSION


def _count_by_scope(rels: Sequence[str], scope: str) -> int:
    return sum(1 for rel in rels if _entry_scope(rel) == scope)


def _collect_config_entries(workspace: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for path in _iter_source_files(workspace):
        entries[_relative_to(workspace, path)] = _encode_file(path)
    return entries


def _group_memory_base(root: Path) -> Path:
    return _workspace_anchor(root) / GROUP_MEMORY_ROOT


def _collect_group_memory_entries(root: Path) -> dict[str, str]:
    """群记忆面：每个群根下的 mirror、sqlite、notes、index。

    队列/ops/review_state、worker lock 与 `.bak` 漂移取证都不进包——它们是运行时状态，
    不是记忆内容。
    """
    base = _group_memory_base(root)
    if not base.is_dir():
        return {}
    entries: dict[str, str] = {}
    for scope_dir in sorted(base.iterdir(), key=lambda item: item.name):
        if not scope_dir.is_dir() or scope_dir.is_symlink() or not admin_segment_ok(scope_dir.name):
            continue
        memory_dir = scope_dir / MEMORY_DIR_ROOT
        prefix = f"{GROUP_MEMORY_ROOT}/{scope_dir.name}"
        mirror = memory_dir / GROUP_MEMORY_MIRROR_NAME
        if mirror.is_file():
            entries[f"{prefix}/{GROUP_MEMORY_MIRROR_NAME}"] = _encode_file(mirror)
        store = memory_dir / GROUP_MEMORY_STATE_DB_NAME
        if store.is_file():
            entries[f"{prefix}/{GROUP_MEMORY_STATE_DB_NAME}"] = _encode_file(store)
        for dirname in GROUP_MEMORY_NOTE_DIRS:
            directory = memory_dir / dirname
            if not directory.is_dir():
                continue
            for child in sorted(directory.iterdir()):
                if child.is_file() and child.suffix in MEMORY_NOTE_SUFFIXES:
                    entries[f"{prefix}/{dirname}/{child.name}"] = _encode_file(child)
    return entries


def _collect_memory_entries(root: Path) -> dict[str, str]:
    land = _memory_landing_paths(root)
    persona_note_name = note_file_name(PERSONA_NOTE_REF)
    entries: dict[str, str] = {}
    for key in (MEMORY_MIRROR_KEY, MEMORY_STATE_DB_KEY):
        path = land[key]
        if path.is_file():
            entries[key] = _encode_file(path)
    if MEMORY_STATE_DB_KEY not in entries:
        # 没有记忆库的"记忆面"会导出一份空知识，导入方却按整包替换理解它。
        raise ValueError("bundle_memory_store_missing")
    for key in (MEMORY_NOTES_KEY, MEMORY_INDEX_KEY):
        directory = land[key]
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            # 人设 note 只随人设面走；`.bak.<stamp>` 是漂移取证，不是记忆。
            if child.name == persona_note_name:
                continue
            if child.is_file() and child.suffix in MEMORY_NOTE_SUFFIXES:
                entries[f"{key}/{child.name}"] = _encode_file(child)
    entries.update(_collect_group_memory_entries(root))
    return entries


def _collect_persona_entries(root: Path) -> dict[str, str]:
    land = _persona_landing_paths(root)
    entries: dict[str, str] = {}
    note = land[PERSONA_NOTE_KEY]
    if note.is_file():
        entries[PERSONA_NOTE_KEY] = _encode_file(note)
    photos = land[PERSONA_PHOTOS_KEY]
    if photos.is_dir():
        for child in sorted(photos.iterdir()):
            if child.is_file() and child.suffix.lower() in PERSONA_PHOTO_SUFFIXES:
                entries[f"{PERSONA_PHOTOS_KEY}/{child.name}"] = _encode_file(child)
    if not entries:
        # 勾了人设却没有任何内容：导出会得到一个"看起来带了人设"的空面，导入方
        # 还会按整包替换清掉目标机的人设，宁可在这里拒绝。
        raise ValueError("bundle_persona_empty")
    return entries


def _collect_sticker_entries(root: Path) -> dict[str, str]:
    """表情包面：操作员的包目录整棵带走，只认 manifest/registry 与分类图片。

    目录里可能有编辑器残留、缩略图缓存之类不属于表情包树的文件；入口与出口都按
    同一份白名单收，保证「导出的包一定导得回去」。

    预置包随仓库分发、由升级维护，不进包：它在目标机上有自己的版本，带上旧版本
    反而会整包覆盖回新装设备。
    """
    base = _stickers_landing_paths(root)[STICKERS_DIR_ROOT]
    entries: dict[str, str] = {}
    builtin_skipped = 0
    if base.is_dir():
        for pack_dir in sorted(base.iterdir(), key=lambda item: item.name):
            if not pack_dir.is_dir() or pack_dir.is_symlink() or not admin_segment_ok(pack_dir.name):
                continue
            if is_builtin_pack(pack_dir.name):
                builtin_skipped += 1
                continue
            for child in sorted(pack_dir.iterdir(), key=lambda item: item.name):
                if child.is_file() and not child.is_symlink() and child.name in STICKER_PACK_FILES:
                    entries[f"{STICKERS_DIR_ROOT}/{pack_dir.name}/{child.name}"] = _encode_file(child)
                elif child.is_dir() and not child.is_symlink() and admin_segment_ok(child.name):
                    for image in sorted(child.iterdir(), key=lambda item: item.name):
                        if image.is_file() and not image.is_symlink() and image.suffix.lower() in IMAGE_SUFFIXES:
                            entries[f"{STICKERS_DIR_ROOT}/{pack_dir.name}/{child.name}/{image.name}"] = _encode_file(image)
    if not entries:
        # 勾了表情包却没有可导出的包：导出会得到一个"看起来带了表情包"的空面，
        # 导入方还会按整面替换动到目标机的包，宁可在这里拒绝。
        if builtin_skipped:
            raise ValueError("bundle_stickers_only_builtin")
        raise ValueError("bundle_stickers_empty")
    return entries


def _collect_entries(workspace: Path, scopes: tuple[str, ...]) -> dict[str, str]:
    entries: dict[str, str] = {}
    if SCOPE_CONFIG in scopes:
        entries.update(_collect_config_entries(workspace))
    if SCOPE_MEMORY in scopes:
        entries.update(_collect_memory_entries(workspace))
    if SCOPE_PERSONA in scopes:
        entries.update(_collect_persona_entries(workspace))
    if SCOPE_STICKERS in scopes:
        entries.update(_collect_sticker_entries(workspace))
    return entries


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


def _encode_file(path: Path) -> str:
    blob = _snapshot_sqlite(path) if path.suffix in SQLITE_SUFFIXES else path.read_bytes()
    return base64.b64encode(blob).decode("ascii")


def _validate_password(password: str) -> str:
    text = str(password or "")
    if not text:
        raise ValueError("password is required")
    return text


def export_bundle(
    workspace: Path | None = None,
    *,
    password: str = "",
    use_project_password: bool = True,
    scopes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Pack the selected面 into one encrypted file.

    `scopes` decides what travels: 配置面 carries the wiring plus the master key,
    记忆面 carries the memory store, its notes and the resident mirror, 人设面
    carries the reserved persona note plus `my_image/`, and 表情包面 carries the
    pack tree under `<workspace>/stickers`. With
    `use_project_password` the bundle key is the live master key and the
    password envelope travels inside the bundle, so the operator types nothing
    here and unlocks the import with their normal project password. The
    envelope is not secret material — it only turns a plaintext password into
    that key — so shipping it keeps the bundle protected by the password.
    """
    clean_scopes = _validate_scopes(scopes if scopes is not None else (SCOPE_CONFIG,))
    root = (workspace or Path.cwd()).resolve()
    service = get_bootstrap_security_service(root)
    master_key = service.active_master_key()
    if not master_key:
        raise ValueError("project is locked")

    salt = b""
    unlock_envelope: dict[str, Any] | None = None
    if use_project_password:
        unlock_envelope = service.master_key_envelope()
        if unlock_envelope is None:
            raise ValueError("project unlock password is not configured")
        bundle_key = master_key
        key_source = KEY_SOURCE_PROJECT_PASSWORD
    else:
        salt = os.urandom(16)
        bundle_key = _key_for(_validate_password(password), salt)
        key_source = KEY_SOURCE_BUNDLE_PASSWORD

    stamp = _stamp()
    created_at = _now_iso()
    entries = _collect_entries(root, clean_scopes)
    payload: dict[str, Any] = {
        "created_at": created_at,
        "workspace_label": root.name,
        "entries": entries,
    }
    if SCOPE_CONFIG in clean_scopes:
        # 只带记忆或人设的包不必再拷一份主密钥：那面根本不动密钥，少一份副本就少一个泄漏面。
        payload["master_key"] = master_key
    token = fernet_from_key(bundle_key).encrypt(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    envelope = {
        "kind": BUNDLE_KIND,
        "version": _bundle_version_for_scopes(clean_scopes),
        # 格式版本决定能不能读，程序版本只用于对照：跨设备排障时"两端构建不一致"
        # 是最常见的原因，光靠 version 字段查不出来。
        "app_version": __version__,
        "created_at": created_at,
        "workspace_label": payload["workspace_label"],
        "scopes": list(clean_scopes),
        "key_source": key_source,
        "kdf": {} if use_project_password else dict(PASSWORD_KDF),
        "salt_b64": base64.b64encode(salt).decode("ascii") if salt else "",
        "unlock_envelope": unlock_envelope,
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
        "app_version": __version__,
        "workspace_label": payload["workspace_label"],
        "key_source": key_source,
        "scopes": list(clean_scopes),
        "entries": sorted(entries),
        "entry_count": len(entries),
        "config_entry_count": _count_by_scope(entries, SCOPE_CONFIG),
        "memory_entry_count": _count_by_scope(entries, SCOPE_MEMORY),
        "persona_entry_count": _count_by_scope(entries, SCOPE_PERSONA),
        "sticker_entry_count": _count_by_scope(entries, SCOPE_STICKERS),
        "bytes": path.stat().st_size,
    }


def _read_envelope(archive_path: Path) -> dict[str, Any]:
    try:
        envelope = json.loads(Path(archive_path).read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("not a config bundle file") from exc
    if not isinstance(envelope, dict) or envelope.get("kind") != BUNDLE_KIND:
        raise ValueError("not a config bundle file")
    if int(envelope.get("version") or 0) not in SUPPORTED_BUNDLE_VERSIONS:
        raise ValueError("unsupported config bundle version")
    return envelope


def _decrypt_payload(envelope: dict[str, Any], password: str) -> dict[str, Any]:
    payload_b64 = str(envelope.get("payload_b64") or "").strip()
    if not payload_b64:
        raise ValueError("invalid config bundle envelope")
    if str(envelope.get("key_source") or "") == KEY_SOURCE_PROJECT_PASSWORD:
        unlock_envelope = envelope.get("unlock_envelope")
        if not isinstance(unlock_envelope, dict):
            raise ValueError("invalid config bundle envelope")
        bundle_key = unwrap_master_key(unlock_envelope, password)
    else:
        salt_b64 = str(envelope.get("salt_b64") or "").strip()
        if not salt_b64:
            raise ValueError("invalid config bundle envelope")
        bundle_key = _key_for(password, base64.b64decode(salt_b64))
    try:
        decrypted = fernet_from_key(bundle_key).decrypt(base64.b64decode(payload_b64))
        payload = json.loads(decrypted.decode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("invalid password") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid config bundle payload")
    return payload


def _safe_relative_path(rel: str) -> PurePosixPath:
    text = str(rel or "").replace("\\", "/").strip()
    parts = PurePosixPath(text).parts
    if not parts or parts[0] not in BUNDLE_ROOTS:
        raise ValueError(f"bundle entry outside package roots rejected: {text}")
    if text.startswith("/") or ".." in parts or ":" in parts[0]:
        raise ValueError(f"unsafe bundle path rejected: {text}")
    return PurePosixPath(*parts)


def _group_memory_entry_target(root: Path, rel: str) -> Path:
    """群记忆逻辑名 → 本机落点；只认固定布局，scope 与文件名都必须安全。"""
    parts = PurePosixPath(rel).parts
    if len(parts) < 4 or parts[0] != MEMORY_DIR_ROOT or parts[1] != "groups":
        raise ValueError(f"unsupported group memory bundle path: {rel}")
    scope = parts[2]
    if not admin_segment_ok(scope):
        raise ValueError(f"unsafe group memory bundle path rejected: {rel}")
    rest = parts[3:]
    base = _group_memory_base(root) / scope / MEMORY_DIR_ROOT
    if len(rest) == 1 and rest[0] in {GROUP_MEMORY_MIRROR_NAME, GROUP_MEMORY_STATE_DB_NAME}:
        return base / rest[0]
    if (
        len(rest) == 2
        and rest[0] in GROUP_MEMORY_NOTE_DIRS
        and rest[1] not in {"", ".", ".."}
        and PurePosixPath(rest[1]).suffix in MEMORY_NOTE_SUFFIXES
    ):
        return base / rest[0] / rest[1]
    raise ValueError(f"unsupported group memory bundle path: {rel}")


def _memory_entry_target(land: dict[str, Path], rel: str, *, root: Path) -> Path:
    """包内逻辑名 → 这台机器的落点；路径由本机清单算，所以不跟包里的字节走。"""
    if rel.startswith(f"{GROUP_MEMORY_ROOT}/"):
        return _group_memory_entry_target(root, rel)
    for key, base in land.items():
        if rel == key:
            if key in (MEMORY_NOTES_KEY, MEMORY_INDEX_KEY):
                raise ValueError(f"memory bundle entry must name a file: {rel}")
            return base
        prefix = f"{key}/"
        if rel.startswith(prefix):
            name = rel[len(prefix):]
            if "/" in name or "\\" in name or ":" in name or name in {"", ".", ".."}:
                raise ValueError(f"unsafe memory bundle path rejected: {rel}")
            return base / name
    raise ValueError(f"unsupported memory bundle path: {rel}")


def _persona_entry_target(land: dict[str, Path], rel: str) -> Path:
    """人设面的逻辑名 → 本机落点；note 走记忆清单，照片固定回 workspace 根。"""
    if rel == PERSONA_NOTE_KEY:
        return land[PERSONA_NOTE_KEY]
    prefix = f"{PERSONA_PHOTOS_KEY}/"
    if rel.startswith(prefix):
        name = rel[len(prefix):]
        if "/" in name or "\\" in name or ":" in name or name in {"", ".", ".."}:
            raise ValueError(f"unsafe persona bundle path rejected: {rel}")
        if PurePosixPath(name).suffix.lower() not in PERSONA_PHOTO_SUFFIXES:
            raise ValueError(f"unsupported persona bundle path: {rel}")
        return land[PERSONA_PHOTOS_KEY] / name
    raise ValueError(f"unsupported persona bundle path: {rel}")


def _stickers_entry_target(land: dict[str, Path], rel: str) -> Path:
    """表情包面的逻辑名 → 本机落点；只认包边车与分类图片两种形状。"""
    base = land[STICKERS_DIR_ROOT]
    parts = PurePosixPath(rel).parts
    if len(parts) < 3 or parts[0] != STICKERS_DIR_ROOT:
        raise ValueError(f"unsupported sticker bundle path: {rel}")
    pack = parts[1]
    rest = parts[2:]
    if not admin_segment_ok(pack):
        raise ValueError(f"unsafe sticker bundle path rejected: {rel}")
    if len(rest) == 1 and rest[0] in STICKER_PACK_FILES:
        return base / pack / rest[0]
    if (
        len(rest) == 2
        and admin_segment_ok(rest[0])
        and PurePosixPath(rest[1]).suffix.lower() in IMAGE_SUFFIXES
    ):
        return base / pack / rest[0] / rest[1]
    raise ValueError(f"unsupported sticker bundle path: {rel}")


def _entry_targets(root: Path, rels: list[str]) -> dict[str, Path]:
    memory_land = _memory_landing_paths(root)
    persona_land = _persona_landing_paths(root, memory_land)
    stickers_land = _stickers_landing_paths(root)
    targets: dict[str, Path] = {}
    for rel in rels:
        pure = _safe_relative_path(rel)
        scope = _entry_scope(str(pure))
        if scope == SCOPE_MEMORY:
            target = _memory_entry_target(memory_land, str(pure), root=root)
        elif scope == SCOPE_PERSONA:
            target = _persona_entry_target(persona_land, str(pure))
        elif scope == SCOPE_STICKERS:
            target = _stickers_entry_target(stickers_land, str(pure))
        else:
            target = root.joinpath(*pure.parts)
        targets[str(pure)] = target
    return targets


def _preflight(payload: dict[str, Any], scopes: tuple[str, ...]) -> tuple[str, list[str]]:
    entries = payload.get("entries")
    if not isinstance(entries, dict) or not entries:
        raise ValueError("bundle carries no files")
    targets = {str(key): _safe_relative_path(str(key)) for key in entries}
    # 声明只有一面、条目却写着另一面：这种包要么是手工改过，要么版本对不上，整包拒。
    foreign = [key for key in targets if _entry_scope(key) not in scopes]
    if foreign:
        raise ValueError(f"bundle entry does not match its declared scopes: {foreign[0]}")
    rels = sorted(str(pure) for pure in targets.values())
    master_key = str(payload.get("master_key") or "").strip()
    if SCOPE_CONFIG not in scopes:
        # 只导入记忆或人设时一个密钥都不动，包里有没有主密钥因此与落盘无关。
        return master_key, rels
    if not master_key:
        raise ValueError("bundle carries no master key")
    overlay = entries.get(f"{SECRET_REALMS_DIR}/default.enc")
    if overlay:
        # 先证伪：钥匙打不开覆盖层就整包拒绝，避免装出一个只读空视图的部署。
        try:
            fernet_from_key(master_key).decrypt(base64.b64decode(str(overlay)))
        except InvalidToken as exc:
            raise ValueError("bundle master key cannot decrypt its secret overlay") from exc
    return master_key, rels


def _backup_targets(targets: dict[str, Path], backup_root: Path) -> list[str]:
    backed_up: list[str] = []
    for rel, source in targets.items():
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


def _restore_backup(backup_root: Path, targets: dict[str, Path], backed_up: list[str]) -> None:
    for rel in backed_up:
        source = backup_root / rel
        target = targets[rel]
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            # 目录只用于人设照片面：回滚要连导入新建的照片一起清掉，不能只覆盖旧文件。
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)


def _write_entries(entries: dict[str, str], targets: dict[str, Path]) -> None:
    for rel, target in targets.items():
        blob = base64.b64decode(str(entries[rel]))
        if PurePosixPath(rel).suffix in SQLITE_SUFFIXES:
            _restore_sqlite(blob, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            os.chmod(target, 0o600)


def _persona_backup_sources(root: Path) -> dict[str, Path]:
    """人设面替换前要整份留底：note 可能被删，照片目录会删掉包外文件。"""
    land = _persona_landing_paths(root)
    sources: dict[str, Path] = {}
    note = land[PERSONA_NOTE_KEY]
    if note.is_file():
        sources[PERSONA_NOTE_KEY] = note
    photos = land[PERSONA_PHOTOS_KEY]
    if photos.is_dir():
        sources[PERSONA_PHOTOS_KEY] = photos
    return sources


def _clean_persona_face(land: dict[str, Path], entries: dict[str, str]) -> None:
    """人设面按包内集合整体替换：描述与照片都不保留包外旧值。"""
    note = land[PERSONA_NOTE_KEY]
    if PERSONA_NOTE_KEY not in entries:
        note.unlink(missing_ok=True)
    photos = land[PERSONA_PHOTOS_KEY]
    if not photos.is_dir():
        return
    # Windows 上写入 `me.png` 会覆盖已有的 `ME.PNG`，目录项仍保留原大小写；
    # 按本机路径语义比较，否则刚写进去的照片会被当成"包外旧图"删掉。
    keep = {
        os.path.normcase(rel[len(PERSONA_PHOTOS_KEY) + 1:])
        for rel in entries
        if rel.startswith(f"{PERSONA_PHOTOS_KEY}/")
    }
    for child in photos.iterdir():
        if not child.is_file():
            continue
        if child.suffix.lower() not in PERSONA_PHOTO_SUFFIXES:
            continue
        if os.path.normcase(child.name) not in keep:
            child.unlink(missing_ok=True)


def _stickers_backup_sources(root: Path) -> dict[str, Path]:
    """表情包面替换前给操作员数据留底：预置包原地保留，不需要备份。"""
    base = _stickers_landing_paths(root)[STICKERS_DIR_ROOT]
    if not base.is_dir():
        return {}
    sources: dict[str, Path] = {}
    for child in base.iterdir():
        if child.is_dir() and not child.is_symlink() and is_builtin_pack(child.name):
            continue
        sources[f"{STICKERS_DIR_ROOT}/{child.name}"] = child
    return sources


def _apply_sticker_scope(root: Path) -> None:
    """清掉操作员包再写入包内容：表情包面按包内集合整体替换。

    预置包随仓库分发，不属于导入范围：原地保留，包内同名条目在导入前已被摘掉。
    """
    base = _stickers_landing_paths(root)[STICKERS_DIR_ROOT]
    if not base.is_dir():
        return
    for child in base.iterdir():
        if child.is_dir() and not child.is_symlink() and is_builtin_pack(child.name):
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
            continue
        try:
            child.unlink()
        except OSError:
            continue


def import_bundle(
    workspace: Path | None = None,
    *,
    archive_path: Path | str,
    password: str,
    current_password: str = "",
    scopes: Sequence[str] | None = None,
) -> dict[str, Any]:
    clean_scopes = _validate_scopes(scopes if scopes is not None else (SCOPE_CONFIG,))
    root = (workspace or Path.cwd()).resolve()
    security = get_bootstrap_security_service(root)
    # 本机已经有解锁口令时，导入先用它证身份，再用它重裹包里的新主密钥：
    # 「导入不改项目口令」是合同，而不是操作员要自己记住的副作用。
    # 没有口令（新装设备）才沿用包口令——那时包口令就是这台机器的第一个口令。
    # 只带记忆或人设同样过这道证明：往这台机写知识/人设必须由知道本机口令的人发起。
    kept_password = ""
    if security.has_installed_envelope():
        clean_current = str(current_password or "")
        if not clean_current:
            raise ValueError("bundle_current_password_required")
        if not security.unwrap_with_password(clean_current):
            raise ValueError("bundle_current_password_invalid")
        kept_password = clean_current
    envelope = _read_envelope(Path(archive_path))
    payload = _decrypt_payload(envelope, _validate_password(password))
    bundle_scopes = _envelope_scopes(envelope)
    missing = [scope for scope in clean_scopes if scope not in bundle_scopes]
    if missing:
        # 勾了包里不存在的一面：宁可报错，也不能装出一个操作员以为"记忆已经带上"的空状态。
        raise ValueError(f"bundle_scope_not_in_package:{missing[0]}")
    master_key, declared_rels = _preflight(payload, bundle_scopes)
    all_entries = payload["entries"]
    applied_rels = [rel for rel in declared_rels if _entry_scope(rel) in clean_scopes]
    # 预置包不属于表情包面：旧版本导出的同名条目整段摘掉，落盘阶段保留目标机自己
    # 的那一份，让升级维护的预置内容不被旧包覆盖。
    builtin_skipped: list[str] = []
    if SCOPE_STICKERS in clean_scopes:
        kept: list[str] = []
        for rel in applied_rels:
            parts = PurePosixPath(rel).parts
            if _entry_scope(rel) == SCOPE_STICKERS and is_builtin_pack(parts[1]):
                if parts[1] not in builtin_skipped:
                    builtin_skipped.append(parts[1])
                continue
            kept.append(rel)
        applied_rels = kept
    # 只剩预置条目的包：整面不动，否则会清掉目标机的自建包却写不进任何东西。
    sticker_apply = SCOPE_STICKERS in clean_scopes and any(
        _entry_scope(rel) == SCOPE_STICKERS for rel in applied_rels
    )
    entries = {rel: all_entries[rel] for rel in applied_rels}
    persona_land = _persona_landing_paths(root)
    targets = _entry_targets(root, applied_rels)

    backup_root = root / IMPORT_BACKUP_DIR / _stamp()
    backup_root.mkdir(parents=True, exist_ok=True)
    # 人设与表情包按集合替换：旧的 note、`my_image/` 与操作员表情包包树先留底；
    # 其余面仍只备份会被覆盖的文件。
    backup_sources = {
        rel: source
        for rel, source in targets.items()
        if _entry_scope(rel) not in {SCOPE_PERSONA, SCOPE_STICKERS}
    }
    if SCOPE_PERSONA in clean_scopes:
        backup_sources.update(_persona_backup_sources(root))
    if sticker_apply:
        backup_sources.update(_stickers_backup_sources(root))
    backed_up = _backup_targets(backup_sources, backup_root)
    status: dict[str, Any] | None = None
    try:
        if sticker_apply:
            _apply_sticker_scope(root)
        _write_entries(entries, targets)
        if SCOPE_PERSONA in clean_scopes:
            _clean_persona_face(persona_land, entries)
        if SCOPE_CONFIG in clean_scopes:
            status = security.install_master_key(
                master_key=master_key,
                password=kept_password or str(password),
            )
    except Exception:
        _restore_backup(backup_root, backup_sources, backed_up)
        raise
    return {
        "entries": sorted(targets),
        "entry_count": len(targets),
        "scopes": list(clean_scopes),
        "config_entry_count": _count_by_scope(targets, SCOPE_CONFIG),
        "memory_entry_count": _count_by_scope(targets, SCOPE_MEMORY),
        "persona_entry_count": _count_by_scope(targets, SCOPE_PERSONA),
        "sticker_entry_count": _count_by_scope(targets, SCOPE_STICKERS),
        "sticker_scope_applied": sticker_apply,
        "sticker_builtin_skipped": sorted(builtin_skipped),
        "backup_dir": str(backup_root),
        "created_at": str(payload.get("created_at") or ""),
        "workspace_label": str(payload.get("workspace_label") or ""),
        "source_app_version": str(envelope.get("app_version") or ""),
        "local_app_version": __version__,
        "key_source": str(envelope.get("key_source") or ""),
        # 没带配置面的包碰不到钥匙，"解锁口令保持不变"对它同样成立，界面照这句念。
        "password_kept": bool(kept_password) or SCOPE_CONFIG not in clean_scopes,
        "status": status,
    }


__all__ = [
    "BUNDLE_EXTENSION",
    "BUNDLE_KIND",
    "BUNDLE_SCOPES",
    "IMPORT_BACKUP_DIR",
    "KEY_SOURCE_BUNDLE_PASSWORD",
    "KEY_SOURCE_PROJECT_PASSWORD",
    "SCOPE_CONFIG",
    "SCOPE_MEMORY",
    "SCOPE_PERSONA",
    "SCOPE_STICKERS",
    "bundle_paths",
    "export_bundle",
    "import_bundle",
]
