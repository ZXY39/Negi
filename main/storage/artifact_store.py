from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from main.ids import new_artifact_id
from main.models import TaskArtifactRecord
from main.protocol import now_iso


class TaskArtifactStore:
    def __init__(self, *, artifact_dir: Path | str, store):
        self._artifact_dir = Path(artifact_dir)
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._store = store
        self._content_index: dict[tuple[str, str], TaskArtifactRecord] = {}

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
        artifact_id = new_artifact_id()
        safe_task_id = task_id.replace(':', '_').replace('/', '_').replace('\\', '_')
        task_dir = self._artifact_dir / safe_task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        safe_artifact_id = artifact_id.replace(':', '_').replace('/', '_').replace('\\', '_')
        path = task_dir / f'{safe_artifact_id}{extension}'
        path.write_text(content, encoding='utf-8')
        record = TaskArtifactRecord(
            artifact_id=artifact_id,
            task_id=task_id,
            node_id=node_id,
            kind=kind,
            title=title,
            path=str(path),
            mime_type=mime_type,
            preview_text=content[:400],
            created_at=now_iso(),
        )
        persisted = self._store.upsert_artifact(record)
        self._content_index[(task_id, content_hash)] = persisted
        append_event = getattr(self._store, 'append_task_event', None)
        if callable(append_event):
            try:
                task_record = self._store.get_task(task_id)
                session_id = str(getattr(task_record, 'session_id', '') or 'web:shared').strip() or 'web:shared'
                append_event(
                    task_id=task_id,
                    session_id=session_id,
                    event_type='task.artifact.added',
                    created_at=record.created_at,
                    payload={'artifact': persisted.model_dump(mode='json')},
                )
            except Exception:
                pass
        return persisted

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
                    shutil.rmtree(path, ignore_errors=True)
                except FileNotFoundError:
                    pass
        shutil.rmtree(self._task_dir(task_id), ignore_errors=True)
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

    @staticmethod
    def _artifact_matches_content(artifact: TaskArtifactRecord, *, content: str, content_hash: str) -> bool:
        if not artifact.path:
            return False
        path = Path(artifact.path)
        if not path.exists() or not path.is_file():
            return False
        try:
            existing = path.read_text(encoding='utf-8')
        except Exception:
            return False
        if hashlib.sha256(existing.encode('utf-8')).hexdigest() != content_hash:
            return False
        return existing == content
