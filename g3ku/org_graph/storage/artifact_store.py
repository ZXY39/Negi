from __future__ import annotations

import shutil
from pathlib import Path

from g3ku.org_graph.ids import new_artifact_id
from g3ku.org_graph.models import ProjectArtifactRecord
from g3ku.org_graph.protocol import now_iso
from g3ku.org_graph.storage.project_store import ProjectStore


class ArtifactStore:
    def __init__(self, *, artifact_dir: Path, project_store: ProjectStore):
        self._artifact_dir = Path(artifact_dir)
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        self._project_store = project_store

    def create_text_artifact(
        self,
        *,
        project_id: str,
        unit_id: str | None,
        kind: str,
        title: str,
        content: str,
        extension: str = '.md',
        mime_type: str = 'text/markdown',
    ) -> ProjectArtifactRecord:
        artifact_id = new_artifact_id()
        safe_project_id = project_id.replace(':', '_').replace('/', '_').replace('\\', '_')
        project_dir = self._artifact_dir / safe_project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        safe_artifact_id = artifact_id.replace(':', '_').replace('/', '_').replace('\\', '_')
        path = project_dir / f'{safe_artifact_id}{extension}'
        path.write_text(content, encoding='utf-8')
        record = ProjectArtifactRecord(
            artifact_id=artifact_id,
            project_id=project_id,
            unit_id=unit_id,
            kind=kind,
            title=title,
            path=str(path),
            mime_type=mime_type,
            preview_text=content[:400],
            created_at=now_iso(),
        )
        return self._project_store.upsert_artifact(record)

    def list_artifacts(self, project_id: str) -> list[ProjectArtifactRecord]:
        return self._project_store.list_artifacts(project_id)

    def delete_artifacts_for_units(self, project_id: str, unit_ids: list[str]) -> None:
        values = {str(item or '').strip() for item in unit_ids if str(item or '').strip()}
        if not values:
            return
        artifacts = [item for item in self.list_artifacts(project_id) if str(item.unit_id or '') in values]
        for artifact in artifacts:
            path = Path(artifact.path) if artifact.path else None
            if path and path.exists():
                try:
                    path.unlink()
                except IsADirectoryError:
                    shutil.rmtree(path, ignore_errors=True)
                except FileNotFoundError:
                    pass
        self._project_store.delete_artifacts_for_units(list(values))

    def _project_dir(self, project_id: str) -> Path:
        safe_project_id = project_id.replace(':', '_').replace('/', '_').replace('\\', '_')
        return self._artifact_dir / safe_project_id

    def delete_project_artifacts(self, project_id: str, artifacts: list[ProjectArtifactRecord] | None = None) -> None:
        for artifact in artifacts or self.list_artifacts(project_id):
            path = Path(artifact.path) if artifact.path else None
            if path and path.exists():
                try:
                    path.unlink()
                except IsADirectoryError:
                    shutil.rmtree(path, ignore_errors=True)
                except FileNotFoundError:
                    pass
        shutil.rmtree(self._project_dir(project_id), ignore_errors=True)



