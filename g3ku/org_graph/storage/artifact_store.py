from __future__ import annotations

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



