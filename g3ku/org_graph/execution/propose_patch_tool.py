from __future__ import annotations

import base64
import difflib
import json
from pathlib import Path
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.agent.tools.filesystem import _resolve_path

_METADATA_START = '### G3KU_PATCH_METADATA ###'
_DIFF_START = '### G3KU_PATCH_DIFF ###'


class ProposeFilePatchTool(Tool):
    def __init__(self, *, artifact_store: Any, project_id: str, unit_id: str | None, workspace: Path | None = None, allowed_dir: Path | None = None):
        self._artifact_store = artifact_store
        self._project_id = project_id
        self._unit_id = unit_id
        self._workspace = workspace
        self._allowed_dir = allowed_dir

    @property
    def name(self) -> str:
        return 'propose_file_patch'

    @property
    def description(self) -> str:
        return (
            'Propose a unified-diff style file patch without modifying the target file. '
            'Use this instead of direct writes when you need a safe, reviewable change artifact.'
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'File path to patch'},
                'old_text': {'type': 'string', 'description': 'Exact text to replace. Must match uniquely.'},
                'new_text': {'type': 'string', 'description': 'Replacement text'},
                'summary': {'type': 'string', 'description': 'Short patch summary'},
            },
            'required': ['path', 'old_text', 'new_text'],
        }

    async def execute(self, path: str, old_text: str, new_text: str, summary: str | None = None, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, self._workspace, self._allowed_dir)
            if not file_path.exists():
                return json.dumps({'success': False, 'error': f'File not found: {path}'}, ensure_ascii=False)
            if not file_path.is_file():
                return json.dumps({'success': False, 'error': f'Not a file: {path}'}, ensure_ascii=False)
            original = file_path.read_text(encoding='utf-8')
            if old_text not in original:
                return json.dumps({'success': False, 'error': f'old_text not found in {path}'}, ensure_ascii=False)
            if original.count(old_text) > 1:
                return json.dumps({'success': False, 'error': f'old_text appears multiple times in {path}; provide a more specific match'}, ensure_ascii=False)
            updated = original.replace(old_text, new_text, 1)
            diff_lines = list(
                difflib.unified_diff(
                    original.splitlines(),
                    updated.splitlines(),
                    fromfile=str(file_path),
                    tofile=str(file_path),
                    lineterm='',
                )
            )
            patch_text = '\n'.join(diff_lines)
            title = summary or f'Patch proposal for {file_path.name}'
            metadata = {
                'path': str(file_path),
                'summary': title,
                'old_text_b64': base64.b64encode(old_text.encode('utf-8')).decode('ascii'),
                'new_text_b64': base64.b64encode(new_text.encode('utf-8')).decode('ascii'),
            }
            artifact_body = f"{_METADATA_START}\n{json.dumps(metadata, ensure_ascii=False)}\n{_DIFF_START}\n{patch_text}\n"
            artifact = self._artifact_store.create_text_artifact(
                project_id=self._project_id,
                unit_id=self._unit_id,
                kind='patch',
                title=title,
                content=artifact_body,
                extension='.patch',
                mime_type='text/x-diff',
            )
            return json.dumps(
                {
                    'success': True,
                    'artifact': artifact.model_dump(mode='json'),
                    'path': str(file_path),
                    'summary': title,
                    'diff_preview': patch_text[:1000],
                },
                ensure_ascii=False,
            )
        except PermissionError as exc:
            return json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({'success': False, 'error': f'Error proposing patch: {exc}'}, ensure_ascii=False)


def parse_patch_artifact(content: str) -> tuple[dict[str, Any], str]:
    text = str(content or '')
    if _METADATA_START not in text or _DIFF_START not in text:
        raise ValueError('Invalid patch artifact format')
    _, remainder = text.split(_METADATA_START, 1)
    metadata_block, diff_block = remainder.split(_DIFF_START, 1)
    metadata = json.loads(metadata_block.strip())
    diff_text = diff_block.strip()
    return metadata, diff_text

