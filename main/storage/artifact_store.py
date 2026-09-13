from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from main.ids import new_artifact_id
from main.models import TaskArtifactRecord
from main.protocol import now_iso
from main.storage.disk_guard import classify_write_error, disk_policies
from main.storage.fs_utils import remove_tree


def read_artifact_text(record: TaskArtifactRecord | None) -> str:
    """统一 artifact 读端：按 content_encoding/.gz 后缀解压；缺失或读失败返回 ''。

    rest.py / navigation.py / runtime_service.apply_patch_artifact 均须走本函数，
    否则 gzip artifact 会在读端显示为 "[二进制文件]" 或乱码。
    """
    if record is None:
        return ''
    raw_path = str(getattr(record, 'path', '') or '').strip()
    if not raw_path:
        return ''
    path = Path(raw_path)
    encoding = str(getattr(record, 'content_encoding', '') or '').strip().lower()
    if not encoding:
        encoding = 'gzip' if path.suffix == '.gz' else 'plain'
    if not path.exists() or not path.is_file():
        return ''
    try:
        if encoding == 'gzip':
            with gzip.open(path, 'rt', encoding='utf-8') as handle:
                return handle.read()
        return path.read_text(encoding='utf-8')
    except Exception:
        return ''


class TaskArtifactStore:
    def __init__(self, *, artifact_dir: Path | str, store):
        self._artifact_dir = Path(artifact_dir)
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._store = store
        self._content_index: dict[tuple[str, str], TaskArtifactRecord] = {}

    def _write_artifact_content(self, *, base_path: Path, text: str) -> tuple[Path, int, str]:
        """写入 artifact 内容；超阈值转 gzip（tmp + 原子 replace）。

        返回 (最终路径, 原始字节数, 'plain'|'gzip')。ENOSPC 不重试，
        直接抛分类后的 DiskFullError——写失败绝不返回指向不存在文件的记录。
        """
        payload_bytes = text.encode('utf-8')
        size = len(payload_bytes)
        threshold = int(disk_policies().artifact_gzip_threshold_bytes)
        path = base_path
        encoding = 'plain'
        if threshold > 0 and size > threshold:
            path = base_path.with_name(base_path.name + '.gz')
            encoding = 'gzip'
        tmp = path.with_name(path.name + '.tmp')
        try:
            if encoding == 'gzip':
                with gzip.open(tmp, 'wt', encoding='utf-8', compresslevel=6) as handle:
                    handle.write(text)
            else:
                tmp.write_text(text, encoding='utf-8')
            tmp.replace(path)
        except OSError as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            raise classify_write_error(exc) from exc
        return path, size, encoding

    def create_text_artifact(
        self,
        *,
        task_id: str,
        node_id: str | None,
        kind: str,
        title: str,
        content: str,
        extension: str = '.md',
        mime_type: str = 'text/markdown',
    ) -> TaskArtifactRecord:
        content_hash = hashlib.sha256(str(content or '').encode('utf-8')).hexdigest()
        existing = self._find_existing_text_artifact(task_id=task_id, content=content, content_hash=content_hash)
        if existing is not None:
            self._content_index[(task_id, content_hash)] = existing
            return existing
        artifact_id, path = self._allocate_artifact_path(task_id=task_id, extension=extension)
        final_path, size_bytes, content_encoding = self._write_artifact_content(base_path=path, text=content)
        record = TaskArtifactRecord(
            artifact_id=artifact_id,
            task_id=task_id,
            node_id=node_id,
            kind=kind,
            title=title,
            path=str(final_path),
            mime_type=mime_type,
            preview_text=content[:400],
            created_at=now_iso(),
            size_bytes=size_bytes,
            content_encoding=content_encoding,
            content_hash=content_hash,
        )
        persisted = self._store.upsert_artifact(record)
        self._content_index[(task_id, content_hash)] = persisted
        self._note_disk_written(task_id, size_bytes)
        self._emit_artifact_added_event(task_id=task_id, record=persisted)
        return persisted

    def create_json_artifact(
        self,
        *,
        task_id: str,
        node_id: str | None,
        kind: str,
        title: str,
        payload,
        extension: str = '.json',
        mime_type: str = 'application/json',
        preview_text: str = '',
    ) -> TaskArtifactRecord:
        artifact_id, path = self._allocate_artifact_path(task_id=task_id, extension=extension)
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        final_path, size_bytes, content_encoding = self._write_artifact_content(base_path=path, text=text)
        record = TaskArtifactRecord(
            artifact_id=artifact_id,
            task_id=task_id,
            node_id=node_id,
            kind=kind,
            title=title,
            path=str(final_path),
            mime_type=mime_type,
            preview_text=str(preview_text or title or '')[:400],
            created_at=now_iso(),
            size_bytes=size_bytes,
            content_encoding=content_encoding,
        )
        persisted = self._store.upsert_artifact(record)
        self._note_disk_written(task_id, size_bytes)
        self._emit_artifact_added_event(task_id=task_id, record=persisted)
        return persisted

    def create_or_replace_singleton_text_artifact(
        self,
        *,
        task_id: str,
        node_id: str | None,
        kind: str,
        title: str,
        content: str,
        extension: str = '.md',
        mime_type: str = 'text/markdown',
    ) -> TaskArtifactRecord:
        normalized_task_id = str(task_id or '').strip()
        normalized_node_id = str(node_id or '').strip() or None
        normalized_kind = str(kind or '').strip()
        existing = self._find_singleton_text_artifact(
            task_id=normalized_task_id,
            node_id=normalized_node_id,
            kind=normalized_kind,
        )
        if existing is None:
            return self.create_text_artifact(
                task_id=normalized_task_id,
                node_id=normalized_node_id,
                kind=normalized_kind,
                title=title,
                content=content,
                extension=extension,
                mime_type=mime_type,
            )

        path = Path(existing.path)
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        if not path.suffix and extension:
            safe_artifact_id = existing.artifact_id.replace(':', '_').replace('/', '_').replace('\\', '_')
            path = path.parent / f'{safe_artifact_id}{extension}'
        # 旧路径若是历史 .gz 而新内容低于阈值（或反之），先按新内容重算目标基路径：
        # 以去后缀的原始名为基准，避免 .gz 反复叠加。
        base_path = path
        if base_path.suffix == '.gz':
            base_path = base_path.with_name(base_path.name[: -len('.gz')])
        final_path, size_bytes, content_encoding = self._write_artifact_content(base_path=base_path, text=content)
        if final_path != path and path.exists():
            try:
                path.unlink()
            except OSError:
                pass
        content_hash = hashlib.sha256(str(content or '').encode('utf-8')).hexdigest()
        updated = existing.model_copy(
            update={
                'node_id': normalized_node_id,
                'kind': normalized_kind,
                'title': title,
                'path': str(final_path),
                'mime_type': mime_type,
                'preview_text': content[:400],
                'created_at': now_iso(),
                'size_bytes': size_bytes,
                'content_encoding': content_encoding,
                'content_hash': content_hash,
            }
        )
        persisted = self._store.upsert_artifact(updated)
        self._drop_content_index_entries(existing.artifact_id)
        self._content_index[(normalized_task_id, content_hash)] = persisted
        # singleton 覆盖写：按新旧 size 差值记账（旧行无 size_bytes 时按全量计）。
        previous_size = int(getattr(existing, 'size_bytes', 0) or 0)
        self._note_disk_written(normalized_task_id, size_bytes - previous_size)
        return persisted

    def _note_disk_written(self, task_id: str, delta_bytes: int) -> None:
        """磁盘治理（P1）增量记账：失败静默（对账 loop 会以目录实测值纠偏）。"""
        delta = int(delta_bytes or 0)
        if not delta:
            return
        bump = getattr(self._store, 'bump_task_disk_usage', None)
        if not callable(bump):
            return
        try:
            bump(task_id, delta)
        except Exception:
            pass

    def _allocate_artifact_path(self, *, task_id: str, extension: str) -> tuple[str, Path]:
        artifact_id = new_artifact_id()
        safe_task_id = task_id.replace(':', '_').replace('/', '_').replace('\\', '_')
        task_dir = self._artifact_dir / safe_task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        safe_artifact_id = artifact_id.replace(':', '_').replace('/', '_').replace('\\', '_')
        path = task_dir / f'{safe_artifact_id}{extension}'
        return artifact_id, path

    def _emit_artifact_added_event(self, *, task_id: str, record: TaskArtifactRecord) -> None:
        append_event = getattr(self._store, 'append_task_event', None)
        if not callable(append_event):
            return
        try:
            task_record = self._store.get_task(task_id)
            session_id = str(getattr(task_record, 'session_id', '') or 'web:shared').strip() or 'web:shared'
            append_event(
                task_id=task_id,
                session_id=session_id,
                event_type='task.artifact.added',
                created_at=record.created_at,
                payload={'artifact': record.model_dump(mode='json')},
            )
        except Exception:
            return

    def list_artifacts(self, task_id: str) -> list[TaskArtifactRecord]:
        return self._store.list_artifacts(task_id)

    def get_artifact(self, artifact_id: str) -> TaskArtifactRecord | None:
        return self._store.get_artifact(artifact_id)

    def delete_artifacts_for_task(self, task_id: str, artifacts: list[TaskArtifactRecord] | None = None) -> None:
        for artifact in artifacts or self.list_artifacts(task_id):
            path = Path(artifact.path) if artifact.path else None
            if path and path.exists():
                try:
                    path.unlink()
                except IsADirectoryError:
                    remove_tree(path)
                except FileNotFoundError:
                    pass
        remove_tree(self._task_dir(task_id))
        self._content_index = {
            key: value
            for key, value in self._content_index.items()
            if key[0] != task_id
        }

    def _task_dir(self, task_id: str) -> Path:
        safe_task_id = task_id.replace(':', '_').replace('/', '_').replace('\\', '_')
        return self._artifact_dir / safe_task_id

    def _find_existing_text_artifact(self, *, task_id: str, content: str, content_hash: str) -> TaskArtifactRecord | None:
        cached = self._content_index.get((task_id, content_hash))
        if cached is not None and self._artifact_matches_content(cached, content=content, content_hash=content_hash):
            return cached
        for artifact in self.list_artifacts(task_id):
            if self._artifact_matches_content(artifact, content=content, content_hash=content_hash):
                self._content_index[(task_id, content_hash)] = artifact
                return artifact
        return None
    def _find_singleton_text_artifact(
        self,
        *,
        task_id: str,
        node_id: str | None,
        kind: str,
    ) -> TaskArtifactRecord | None:
        normalized_node_id = str(node_id or '').strip() or None
        normalized_kind = str(kind or '').strip()
        for artifact in self.list_artifacts(task_id):
            if str(getattr(artifact, 'kind', '') or '').strip() != normalized_kind:
                continue
            artifact_node_id = str(getattr(artifact, 'node_id', '') or '').strip() or None
            if artifact_node_id != normalized_node_id:
                continue
            return artifact
        return None

    def _drop_content_index_entries(self, artifact_id: str) -> None:
        normalized_artifact_id = str(artifact_id or '').strip()
        if not normalized_artifact_id:
            return
        self._content_index = {
            key: value
            for key, value in self._content_index.items()
            if str(getattr(value, 'artifact_id', '') or '').strip() != normalized_artifact_id
        }

    @staticmethod
    def _artifact_matches_content(artifact: TaskArtifactRecord, *, content: str, content_hash: str) -> bool:
        if not artifact.path:
            return False
        # 快路径：新记录携带 content_hash，直接比对，免回读文件（gz 无需解压）。
        recorded_hash = str(getattr(artifact, 'content_hash', '') or '').strip()
        if recorded_hash:
            if recorded_hash != content_hash:
                return False
            path = Path(artifact.path)
            return path.exists() and path.is_file()
        # 旧行兜底（content_hash 为空）：按统一读端解压回读比对。
        path = Path(artifact.path)
        if not path.exists() or not path.is_file():
            return False
        try:
            existing = read_artifact_text(artifact)
        except Exception:
            return False
        if not existing:
            return False
        if hashlib.sha256(existing.encode('utf-8')).hexdigest() != content_hash:
            return False
        return existing == content
