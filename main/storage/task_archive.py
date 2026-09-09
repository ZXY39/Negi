"""任务级 zip 归档服务（P2 磁盘治理：压缩渐进）。

把一个任务的四个磁盘目录（artifacts / event-history / files / temp）压成单个
zip 归档，源目录删空；解压走反向流程。设计约束：

- **提交点协议**：先写完整 zip（tmp + 校验 + 原子 replace）再逐文件删源——
  任意中断点都满足"zip 有效 = 权威"，``recover_interrupted`` 据此补齐或回滚。
- **压完即删源**：避免压缩期间双倍占盘（磁盘近满时这是硬约束）。
- **解压预检**：``uncompressed_bytes × 1.2 < free`` 才允许解压。
- **排他**：目录级 O_EXCL 文件锁（pid + 过期回收）；调用方（runtime_service）
  另以 DB metadata 条件做跨进程排他。
- 读端回退：``zip_member_text`` 供 ``artifact_store.read_artifact_text`` 在
  源文件缺失时从归档读取成员内容（带容量上限的成员文本缓存）。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from main.storage.disk_guard import disk_waterline_snapshot

__all__ = ['ArchiveResult', 'TaskArchiver']

ARCHIVE_MANIFEST_NAME = '.g3ku-archive.json'
_LOCK_STALE_SECONDS = 300.0
_MEMBER_CACHE_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ArchiveResult:
    task_id: str
    archive_path: str
    uncompressed_bytes: int
    compressed_bytes: int
    file_count: int
    per_dir: dict[str, dict[str, int]] = field(default_factory=dict)


def _safe_task_dir_name(task_id: str) -> str:
    return str(task_id or '').strip().replace(':', '_').replace('/', '_').replace('\\', '_')


class TaskArchiver:
    def __init__(self, *, archive_dir: Path | str) -> None:
        self._archive_dir = Path(archive_dir)
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        self._member_cache_lock = threading.Lock()
        self._member_cache: dict[str, tuple[float, str, int]] = {}
        self._member_cache_bytes = 0

    # ------------------------------------------------------------------
    # 路径与探测
    # ------------------------------------------------------------------

    def archive_path_for(self, task_id: str) -> Path:
        return self._archive_dir / f'{_safe_task_dir_name(task_id)}.zip'

    def has_archive(self, task_id: str) -> bool:
        return self.archive_path_for(task_id).is_file()

    def read_manifest(self, task_id: str) -> dict | None:
        archive_path = self.archive_path_for(task_id)
        if not archive_path.is_file():
            return None
        try:
            with zipfile.ZipFile(archive_path, 'r') as handle:
                with handle.open(ARCHIVE_MANIFEST_NAME) as member:
                    payload = json.loads(member.read().decode('utf-8'))
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    def estimate_uncompressed(self, task_id: str) -> int:
        manifest = self.read_manifest(task_id)
        if not manifest:
            return 0
        try:
            return int(manifest.get('uncompressed_bytes') or 0)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------
    # 压缩
    # ------------------------------------------------------------------

    def archive(self, task_id: str, dirs: dict[str, Path]) -> ArchiveResult | None:
        """把 dirs（key → 任务专属目录）压进单个 zip 并删空源目录。

        返回 None 表示跳过（锁被占、无内容、写失败）。失败时源目录保持可用：
        zip 未通过校验前绝不删源。
        """
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return None
        sources: dict[str, Path] = {}
        for key, raw in (dirs or {}).items():
            path = Path(raw) if raw else None
            if path is not None and path.exists() and path.is_dir():
                sources[str(key)] = path
        if not sources:
            return None
        lock = self._acquire_lock(normalized_task_id)
        if lock is None:
            return None
        archive_path = self.archive_path_for(normalized_task_id)
        tmp_path = archive_path.with_name(f'.tmp-{os.getpid()}-{archive_path.name}')
        try:
            members: list[dict[str, object]] = []
            per_dir: dict[str, dict[str, int]] = {}
            uncompressed_bytes = 0
            file_count = 0
            with zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as handle:
                for key, root in sources.items():
                    dir_files = 0
                    dir_bytes = 0
                    for file_path in sorted(root.rglob('*')):
                        if not file_path.is_file():
                            continue
                        relative = file_path.relative_to(root).as_posix()
                        member_name = f'{key}/{relative}'
                        try:
                            size = int(file_path.stat().st_size)
                            handle.write(file_path, member_name)
                        except OSError:
                            continue
                        members.append({'name': member_name, 'size': size})
                        dir_files += 1
                        dir_bytes += size
                        uncompressed_bytes += size
                        file_count += 1
                    per_dir[key] = {'files': dir_files, 'bytes': dir_bytes}
                manifest = {
                    'task_id': normalized_task_id,
                    'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
                    'uncompressed_bytes': uncompressed_bytes,
                    'file_count': file_count,
                    'per_dir': per_dir,
                    'members': members,
                    'tool_version': 1,
                }
                handle.writestr(ARCHIVE_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False))
            # 校验：读回清单 + 全成员 CRC 检查
            with zipfile.ZipFile(tmp_path, 'r') as handle:
                if handle.testzip() is not None:
                    return None
                with handle.open(ARCHIVE_MANIFEST_NAME) as member:
                    verify = json.loads(member.read().decode('utf-8'))
                if not isinstance(verify, dict) or int(verify.get('file_count') or -1) != file_count:
                    return None
            compressed_bytes = int(tmp_path.stat().st_size)
            os.replace(tmp_path, archive_path)
        except OSError:
            self._unlink_quiet(tmp_path)
            return None
        finally:
            self._release_lock(lock)
        # 提交点之后：逐文件删源（zip 已是权威）
        for root in sources.values():
            self._remove_tree_files(root)
        self.invalidate_member_cache(normalized_task_id)
        return ArchiveResult(
            task_id=normalized_task_id,
            archive_path=str(archive_path),
            uncompressed_bytes=uncompressed_bytes,
            compressed_bytes=compressed_bytes,
            file_count=file_count,
            per_dir=per_dir,
        )

    # ------------------------------------------------------------------
    # 解压
    # ------------------------------------------------------------------

    def decompress(self, task_id: str, dirs: dict[str, Path], *, free_factor: float = 1.2) -> bool:
        """解压回 dirs；预检 uncompressed×free_factor < 磁盘剩余。幂等：无归档返回 True。"""
        normalized_task_id = str(task_id or '').strip()
        archive_path = self.archive_path_for(normalized_task_id)
        if not archive_path.is_file():
            return True
        uncompressed = self.estimate_uncompressed(normalized_task_id)
        snapshot = disk_waterline_snapshot([str(self._archive_dir), *(str(p) for p in (dirs or {}).values() if p)])
        if snapshot is not None and uncompressed > 0:
            free, _total = snapshot
            if free < int(uncompressed * max(1.0, float(free_factor))):
                return False
        lock = self._acquire_lock(normalized_task_id)
        if lock is None:
            return False
        staging_root = self._archive_dir / f'.extract-{_safe_task_dir_name(normalized_task_id)}-{os.getpid()}'
        try:
            with zipfile.ZipFile(archive_path, 'r') as handle:
                if handle.testzip() is not None:
                    return False
                manifest = None
                for member in handle.infolist():
                    if member.filename == ARCHIVE_MANIFEST_NAME:
                        continue
                    key, _, relative = member.filename.partition('/')
                    if not key or not relative:
                        continue
                    target_root = Path(dirs.get(key) or '')
                    if not str(target_root):
                        continue
                    staged = staging_root / key / relative
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    with handle.open(member) as src, open(staged, 'wb') as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                try:
                    with handle.open(ARCHIVE_MANIFEST_NAME) as member:
                        manifest = json.loads(member.read().decode('utf-8'))
                except Exception:
                    manifest = None
                # 校验：逐成员比对大小
                if isinstance(manifest, dict):
                    expected = {
                        str(item.get('name')): int(item.get('size') or 0)
                        for item in list(manifest.get('members') or [])
                        if isinstance(item, dict)
                    }
                    for name, size in expected.items():
                        key, _, relative = name.partition('/')
                        staged = staging_root / key / relative
                        if not staged.is_file() or int(staged.stat().st_size) != size:
                            return False
            # 落位：staging → 目标目录（逐文件 move，兼容目标已存在）
            for key, target_root in (dirs or {}).items():
                staged_root = staging_root / str(key)
                if not staged_root.exists():
                    continue
                target = Path(target_root)
                target.mkdir(parents=True, exist_ok=True)
                for file_path in staged_root.rglob('*'):
                    if not file_path.is_file():
                        continue
                    destination = target / file_path.relative_to(staged_root)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(file_path), str(destination))
            self._unlink_quiet(archive_path)
            self.invalidate_member_cache(normalized_task_id)
            return True
        except (OSError, zipfile.BadZipFile):
            return False
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
            self._release_lock(lock)

    def recover_interrupted(self, task_id: str, dirs: dict[str, Path]) -> str:
        """中断恢复：zip 有效=权威（补删源残留），zip 无效=删除（源完好）。"""
        normalized_task_id = str(task_id or '').strip()
        archive_path = self.archive_path_for(normalized_task_id)
        if not archive_path.is_file():
            return 'none'
        try:
            with zipfile.ZipFile(archive_path, 'r') as handle:
                valid = handle.testzip() is None and ARCHIVE_MANIFEST_NAME in handle.namelist()
        except Exception:
            valid = False
        if not valid:
            self._unlink_quiet(archive_path)
            return 'rolled_back'
        for root in (dirs or {}).values():
            path = Path(root) if root else None
            if path is not None and path.exists():
                self._remove_tree_files(path)
        return 'completed'

    def cleanup_stale_work_files(self, *, max_age_seconds: float = 600.0) -> dict[str, int]:
        """清理闪退残留：.tmp-*.zip（压缩写一半）与 .extract-*（解压 staging）。

        只删超过 max_age_seconds 的，避免误删在途工作文件。
        """
        removed_tmp = 0
        removed_extract = 0
        cutoff = time.time() - max(0.0, float(max_age_seconds))
        try:
            entries = list(self._archive_dir.iterdir())
        except OSError:
            return {'tmp_zips': 0, 'extract_dirs': 0}
        for entry in entries:
            try:
                name = entry.name
                if not name.startswith('.tmp-') and not name.startswith('.extract-'):
                    continue
                if entry.stat().st_mtime >= cutoff:
                    continue
                if entry.is_file() and name.endswith('.zip'):
                    entry.unlink(missing_ok=True)
                    removed_tmp += 1
                elif entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                    removed_extract += 1
            except OSError:
                continue
        return {'tmp_zips': removed_tmp, 'extract_dirs': removed_extract}

    # ------------------------------------------------------------------
    # 归档成员读取（read_artifact_text 回退用）
    # ------------------------------------------------------------------

    def zip_member_bytes(self, task_id: str, member_name: str) -> bytes | None:
        normalized_task_id = str(task_id or '').strip()
        member = str(member_name or '').strip()
        if not normalized_task_id or not member:
            return None
        cache_key = f'{normalized_task_id}\x00{member}'
        with self._member_cache_lock:
            cached = self._member_cache.get(cache_key)
            if cached is not None:
                return cached[1]
        archive_path = self.archive_path_for(normalized_task_id)
        if not archive_path.is_file():
            return None
        try:
            with zipfile.ZipFile(archive_path, 'r') as handle:
                with handle.open(member) as fh:
                    data = fh.read()
        except Exception:
            return None
        if not isinstance(data, bytes):
            return None
        size = len(data)
        with self._member_cache_lock:
            self._member_cache[cache_key] = (time.monotonic(), data, size)
            self._member_cache_bytes += size
            if self._member_cache_bytes > _MEMBER_CACHE_MAX_BYTES:
                for key in sorted(self._member_cache, key=lambda k: self._member_cache[k][0]):
                    _ts, _value, entry_size = self._member_cache.pop(key)
                    self._member_cache_bytes -= entry_size
                    if self._member_cache_bytes <= _MEMBER_CACHE_MAX_BYTES // 2:
                        break
        return data

    def zip_member_text(self, task_id: str, member_name: str) -> str | None:
        data = self.zip_member_bytes(task_id, member_name)
        if data is None:
            return None
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return None

    def invalidate_member_cache(self, task_id: str) -> None:
        prefix = f'{str(task_id or "").strip()}\x00'
        with self._member_cache_lock:
            for key in [k for k in self._member_cache if k.startswith(prefix)]:
                _ts, _value, size = self._member_cache.pop(key)
                self._member_cache_bytes -= size

    def list_archived_members(self, task_id: str, prefix: str = '') -> list[str]:
        manifest = self.read_manifest(task_id)
        if not manifest:
            return []
        names = [
            str(item.get('name') or '')
            for item in list(manifest.get('members') or [])
            if isinstance(item, dict)
        ]
        normalized_prefix = str(prefix or '').strip()
        if not normalized_prefix:
            return names
        return [name for name in names if name.startswith(normalized_prefix)]

    def delete_archive(self, task_id: str) -> bool:
        """P3 删除渐进用：删归档 zip 本体。"""
        archive_path = self.archive_path_for(task_id)
        if not archive_path.is_file():
            return False
        self._unlink_quiet(archive_path)
        self.invalidate_member_cache(str(task_id or '').strip())
        return True

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _acquire_lock(self, task_id: str) -> Path | None:
        lock_path = self._archive_dir / f'.g3ku-archive-{_safe_task_dir_name(task_id)}.lock'
        try:
            if lock_path.exists():
                age = time.time() - lock_path.stat().st_mtime
                if age < _LOCK_STALE_SECONDS:
                    return None
                lock_path.unlink(missing_ok=True)
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode('utf-8'))
            finally:
                os.close(fd)
            return lock_path
        except OSError:
            return None

    @staticmethod
    def _release_lock(lock_path: Path | None) -> None:
        if lock_path is None:
            return
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _unlink_quiet(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    @staticmethod
    def _remove_tree_files(root: Path) -> None:
        """删空目录树（逐文件 unlink + 自底向上 rmdir），失败静默跳过。"""
        try:
            for path in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
                try:
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                except OSError:
                    continue
            root.rmdir()
        except OSError:
            pass
