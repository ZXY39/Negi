from __future__ import annotations

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
        return self._store.upsert_artifact(record)

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

    def _task_dir(self, task_id: str) -> Path:
        safe_task_id = task_id.replace(':', '_').replace('/', '_').replace('\\', '_')
        return self._artifact_dir / safe_task_id
