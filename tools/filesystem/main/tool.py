from __future__ import annotations

import base64
import difflib
import json
from pathlib import Path
from typing import Any

_METADATA_START = '### G3KU_PATCH_METADATA ###'
_DIFF_START = '### G3KU_PATCH_DIFF ###'


def _resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute() and workspace is not None:
        candidate = workspace / candidate
    resolved = candidate.resolve()
    if allowed_dir is not None:
        try:
            resolved.relative_to(allowed_dir.resolve())
        except ValueError as exc:
            raise PermissionError(f'Path {path} is outside allowed directory {allowed_dir}') from exc
    return resolved


def _runtime_task_id(runtime: dict[str, Any] | None, default: str | None = None) -> str:
    payload = runtime if isinstance(runtime, dict) else {}
    task_id = str(payload.get('task_id') or payload.get('project_id') or '').strip()
    if task_id:
        return task_id
    session_key = str(payload.get('session_key') or '').strip() or 'shared'
    fallback = str(default or '').strip()
    return fallback or f'adhoc:{session_key}'


def _runtime_node_id(runtime: dict[str, Any] | None, default: str | None = None) -> str | None:
    payload = runtime if isinstance(runtime, dict) else {}
    node_id = str(payload.get('node_id') or payload.get('unit_id') or '').strip()
    if node_id:
        return node_id
    fallback = str(default or '').strip()
    return fallback or None


class FilesystemTool:
    def __init__(
        self,
        *,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
        artifact_store: Any = None,
        main_task_service: Any = None,
    ) -> None:
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._artifact_store = artifact_store
        self._main_task_service = main_task_service

    async def execute(
        self,
        action: str,
        path: str,
        content: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        summary: str | None = None,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        operation = str(action or '').strip().lower()
        if not operation:
            return 'Error: action is required'
        denied = self._authorize(operation, runtime)
        if denied is not None:
            return denied
        try:
            if operation == 'read':
                return self._read(path)
            if operation == 'list':
                return self._list(path)
            if operation == 'write':
                return self._write(path, content)
            if operation == 'edit':
                return self._edit(path, old_text, new_text)
            if operation == 'delete':
                return self._delete(path)
            if operation == 'propose_patch':
                return self._propose_patch(path, old_text, new_text, summary, runtime)
            return f'Error: Unsupported filesystem action: {operation}'
        except PermissionError as exc:
            return f'Error: {exc}'
        except Exception as exc:
            return f'Error executing filesystem action {operation}: {exc}'

    def _authorize(self, action_id: str, runtime: dict[str, Any]) -> str | None:
        service = self._main_task_service
        checker = getattr(service, 'is_tool_action_allowed', None) if service is not None else None
        if checker is None:
            return None
        actor_role = str(runtime.get('actor_role') or 'ceo').strip().lower() or 'ceo'
        session_id = str(runtime.get('session_key') or 'web:shared').strip() or 'web:shared'
        allowed = checker(
            actor_role=actor_role,
            session_id=session_id,
            tool_id='filesystem',
            action_id=action_id,
            task_id=str(runtime.get('task_id') or '').strip() or None,
            node_id=str(runtime.get('node_id') or '').strip() or None,
        )
        if allowed:
            return None
        return f'Error: Action not allowed for role {actor_role}: filesystem.{action_id}'

    def _read(self, path: str) -> str:
        file_path = _resolve_path(path, self._workspace, self._allowed_dir)
        if not file_path.exists():
            return f'Error: File not found: {path}'
        if not file_path.is_file():
            return f'Error: Not a file: {path}'
        return file_path.read_text(encoding='utf-8')

    def _list(self, path: str) -> str:
        dir_path = _resolve_path(path, self._workspace, self._allowed_dir)
        if not dir_path.exists():
            return f'Error: Directory not found: {path}'
        if not dir_path.is_dir():
            return f'Error: Not a directory: {path}'
        items = [f'{"DIR " if item.is_dir() else "FILE"} {item.name}' for item in sorted(dir_path.iterdir())]
        if not items:
            return f'Directory {path} is empty'
        return '\n'.join(items)

    def _write(self, path: str, content: str | None) -> str:
        if content is None:
            return 'Error: content is required when action=write'
        file_path = _resolve_path(path, self._workspace, self._allowed_dir)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding='utf-8')
        return f'Successfully wrote {len(content)} bytes to {file_path}'

    def _edit(self, path: str, old_text: str | None, new_text: str | None) -> str:
        if old_text is None:
            return 'Error: old_text is required when action=edit'
        if new_text is None:
            return 'Error: new_text is required when action=edit'
        file_path = _resolve_path(path, self._workspace, self._allowed_dir)
        if not file_path.exists():
            return f'Error: File not found: {path}'
        if not file_path.is_file():
            return f'Error: Not a file: {path}'
        content = file_path.read_text(encoding='utf-8')
        if old_text not in content:
            return self._not_found_message(old_text, content, path)
        count = content.count(old_text)
        if count > 1:
            return f'Warning: old_text appears {count} times. Please provide more context to make it unique.'
        updated = content.replace(old_text, new_text, 1)
        file_path.write_text(updated, encoding='utf-8')
        return f'Successfully edited {file_path}'

    def _delete(self, path: str) -> str:
        file_path = _resolve_path(path, self._workspace, self._allowed_dir)
        if not file_path.exists():
            return f'Error: File not found: {path}'
        if not file_path.is_file():
            return f'Error: Not a file: {path}'
        file_path.unlink()
        return f'Successfully deleted {file_path}'

    def _propose_patch(
        self,
        path: str,
        old_text: str | None,
        new_text: str | None,
        summary: str | None,
        runtime: dict[str, Any],
    ) -> str:
        if old_text is None:
            return json.dumps({'success': False, 'error': 'old_text is required when action=propose_patch'}, ensure_ascii=False)
        if new_text is None:
            return json.dumps({'success': False, 'error': 'new_text is required when action=propose_patch'}, ensure_ascii=False)
        if self._artifact_store is None:
            return json.dumps({'success': False, 'error': 'Patch artifact store is unavailable'}, ensure_ascii=False)
        file_path = _resolve_path(path, self._workspace, self._allowed_dir)
        if not file_path.exists():
            return json.dumps({'success': False, 'error': f'File not found: {path}'}, ensure_ascii=False)
        if not file_path.is_file():
            return json.dumps({'success': False, 'error': f'Not a file: {path}'}, ensure_ascii=False)
        original = file_path.read_text(encoding='utf-8')
        if old_text not in original:
            return json.dumps({'success': False, 'error': f'old_text not found in {path}'}, ensure_ascii=False)
        if original.count(old_text) > 1:
            return json.dumps(
                {'success': False, 'error': f'old_text appears multiple times in {path}; provide a more specific match'},
                ensure_ascii=False,
            )
        updated = original.replace(old_text, new_text, 1)
        patch_text = '\n'.join(
            difflib.unified_diff(
                original.splitlines(),
                updated.splitlines(),
                fromfile=str(file_path),
                tofile=str(file_path),
                lineterm='',
            )
        )
        title = summary or f'Patch proposal for {file_path.name}'
        metadata = {
            'path': str(file_path),
            'summary': title,
            'old_text_b64': base64.b64encode(old_text.encode('utf-8')).decode('ascii'),
            'new_text_b64': base64.b64encode(new_text.encode('utf-8')).decode('ascii'),
        }
        artifact_body = f'{_METADATA_START}\n{json.dumps(metadata, ensure_ascii=False)}\n{_DIFF_START}\n{patch_text}\n'
        artifact = self._artifact_store.create_text_artifact(
            task_id=_runtime_task_id(runtime),
            node_id=_runtime_node_id(runtime),
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

    @staticmethod
    def _not_found_message(old_text: str, content: str, path: str) -> str:
        lines = content.splitlines(keepends=True)
        old_lines = old_text.splitlines(keepends=True)
        window = len(old_lines)
        best_ratio, best_start = 0.0, 0
        for index in range(max(1, len(lines) - window + 1)):
            ratio = difflib.SequenceMatcher(None, old_lines, lines[index : index + window]).ratio()
            if ratio > best_ratio:
                best_ratio, best_start = ratio, index
        if best_ratio > 0.5:
            diff = '\n'.join(
                difflib.unified_diff(
                    old_lines,
                    lines[best_start : best_start + window],
                    fromfile='old_text (provided)',
                    tofile=f'{path} (actual, line {best_start + 1})',
                    lineterm='',
                )
            )
            return f'Error: old_text not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}'
        return f'Error: old_text not found in {path}. No similar text found. Verify the file content.'


def build(runtime):
    loop = getattr(runtime, 'loop', None)
    allowed_dir = runtime.workspace if getattr(loop, 'restrict_to_workspace', False) else None
    service = getattr(runtime.services, 'main_task_service', None)
    artifact_store = getattr(service, 'artifact_store', None) if service is not None else None
    return FilesystemTool(
        workspace=runtime.workspace,
        allowed_dir=allowed_dir,
        artifact_store=artifact_store,
        main_task_service=service,
    )
