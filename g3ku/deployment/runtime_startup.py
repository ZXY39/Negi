from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger

from g3ku.deployment.data_root import data_work_path
from g3ku.security import BOOTSTRAP_MASTER_KEY_ENV, get_bootstrap_security_service
from g3ku.utils.helpers import ensure_dir, sync_workspace_templates

BOOTSTRAP_PASSWORD_ENV = "G3KU_BOOTSTRAP_PASSWORD"
RESOURCE_SEED_ROOT_ENV = "G3KU_RESOURCE_SEED_ROOT"
RUNTIME_TEMP_DIR_PARTS = ("temp",)
PERSISTENT_WORKSPACE_DIRS = (
    ".g3ku",
    "memory",
    "sessions",
    "externaltools",
    "skills",
    "tools",
)


def runtime_temp_dir() -> Path:
    """数据根一侧的临时工作区，与 `ensure_persistent_workspace_dirs` 建的是同一个目录。"""
    return ensure_dir(data_work_path(*RUNTIME_TEMP_DIR_PARTS))


def anchor_process_temp_dir() -> str:
    """把进程的临时目录指向数据根的 `temp/`；返回锚到的目录，锚不上返回空串并保持继承到的环境不动。

    SQLite 在 Windows 上按 `TMP` / `TEMP` 找临时目录，排序超过内存就在那儿建文件。建不出来
    报的是 `SQLITE_CANTOPEN`，而异常文本里印的是**库文件的绝对路径**，长得像"库打不开"：
    带 `ORDER BY` 的读（worker 状态、节点表、磁盘摘要、`perf_samples`）会稳定失败，而主键
    点查照旧成功。启动者的环境里那个目录可能不可写（容器、被安全策略拦下的解释器、被清理
    过的 `%TEMP%`），所以落点由运行时自己定，不继承调用者。

    可写性用真建一个文件判：`os.access(dir, W_OK)` 只看 ACL，过滤驱动拦截下它仍返回 True。
    锚不上就保持继承到的环境不动并打一行——数据目录本身只读时，把 `TMP` 指过去只会把一个
    可能还能用的环境换成一定能坏的。
    """
    target: Path | None = None
    try:
        target = runtime_temp_dir()
        probe = target / ".g3ku-write-probe"
        probe.write_text("probe", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        shown = str(target) if target is not None else ""
        logger.warning(
            f"Runtime temp anchor skipped: data-root temp not writable path={shown} "
            f"{type(exc).__name__}:{getattr(exc, 'errno', '')}"
        )
        return ""
    value = str(target)
    for name in ("TMP", "TEMP", "TMPDIR"):
        os.environ[name] = value
    tempfile.tempdir = value
    logger.info(f"Runtime temp anchored: {value}")
    return value


def ensure_persistent_workspace_dirs(workspace: Path) -> dict[str, str]:
    root = Path(workspace).resolve()
    created: dict[str, str] = {}
    for relative in PERSISTENT_WORKSPACE_DIRS:
        path = ensure_dir(root / relative)
        created[relative] = str(path)
    # temp 挂数据根而不是工作区：在安装根 mkdir 一个空 temp 会让"临时文件已搬走"
    # 看上去没实现。默认解析（未设数据目录）时两者同路径，容器与存量安装不受影响。
    temp_path = ensure_dir(data_work_path("temp"))
    created["temp"] = str(temp_path)
    sync_workspace_templates(root, silent=True)
    return created


def _resolved_seed_root(seed_root: Path | None = None) -> Path | None:
    if seed_root is not None:
        return Path(seed_root).expanduser().resolve(strict=False)
    raw = str(os.getenv(RESOURCE_SEED_ROOT_ENV, "") or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve(strict=False)


def seed_workspace_resources(workspace: Path, *, seed_root: Path | None = None) -> dict[str, list[str]]:
    root = Path(workspace).resolve()
    source_root = _resolved_seed_root(seed_root)
    copied: dict[str, list[str]] = {"skills": [], "tools": []}
    if source_root is None or not source_root.exists():
        return copied

    for kind in ("skills", "tools"):
        source_dir = source_root / kind
        target_dir = root / kind
        if not source_dir.exists():
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        for source_path in source_dir.rglob("*"):
            if not source_path.is_file():
                continue
            relative = source_path.relative_to(source_dir)
            target_path = target_dir / relative
            if target_path.exists():
                continue
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)
            copied[kind].append(relative.as_posix())
    return copied


def auto_unlock_from_env(
    *,
    workspace: Path | None = None,
    security_service: Any | None = None,
) -> str:
    security = security_service or get_bootstrap_security_service(workspace)
    # One audit per process: keep a fresh copy of the key material, or say out loud (and in
    # .g3ku/security-events.jsonl) that material which used to exist is gone. Without this a
    # lost credential store looked identical to a never-configured project.
    try:
        finding = security.audit_key_store()
        if str((finding or {}).get("state") or "") == "lost":
            logger.error(
                "key store material missing; newest backup: {}",
                str((finding or {}).get("newest_backup") or "(no backup)"),
            )
    except Exception as exc:  # never let the audit block a boot
        logger.warning(f"key store audit failed: {exc}")
    if security.is_unlocked():
        return "already_unlocked"

    master_key = str(os.getenv(BOOTSTRAP_MASTER_KEY_ENV, "") or "").strip()
    if not master_key:
        # 操作者在设置里勾过「记住密码自动解锁」时，钥匙落在 .g3ku/llm-config 里。
        master_key = str(security.auto_unlock_master_key() or "").strip()
    if master_key:
        security.activate_with_master_key(master_key=master_key)
        return "master_key"

    password = str(os.getenv(BOOTSTRAP_PASSWORD_ENV, "") or "").strip()
    if password and str((security.status() or {}).get("mode") or "").strip() == "locked":
        security.unlock(password=password)
        return "password"

    return ""
