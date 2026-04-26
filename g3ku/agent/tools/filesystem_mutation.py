from __future__ import annotations

import asyncio
import base64
import difflib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from g3ku.resources.tool_settings import FilesystemToolSettings, runtime_tool_settings
from g3ku.utils.subprocess_text import decode_subprocess_output, enrich_subprocess_env_for_text

_METADATA_START = '### G3KU_PATCH_METADATA ###'
_DIFF_START = '### G3KU_PATCH_DIFF ###'
_EDIT_MODE_TEXT = 'text_replace'
_EDIT_MODE_RANGE = 'line_range'
_EDIT_MODE_ERROR = 'Error: edit requires exactly one mode: text-replace or line-range'


def _content_ref_path_error(path: str) -> str | None:
    raw = str(path or '').strip()
    if not raw:
        return None
    if raw.startswith('artifact:'):
        return f'content ref is not a filesystem path: {path}; use the content tool with ref={raw}'
    return None


def _raise_if_content_ref_path(path: str) -> None:
    content_ref_error = _content_ref_path_error(path)
    if content_ref_error is not None:
        raise ValueError(content_ref_error)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dir: Path | None = None,
) -> Path:
    _raise_if_content_ref_path(path)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f'relative path is not allowed; provide absolute path: {path}')
    resolved = candidate.resolve()
    if allowed_dir is not None:
        try:
            resolved.relative_to(allowed_dir.resolve())
        except ValueError as exc:
            raise PermissionError(f'Path {path} is outside allowed directory {allowed_dir}') from exc
    return resolved


def _normalize_edit_mode(value: Any) -> str:
    text = str(value or '').strip().lower()
    if text in {'text-replace', 'text_replace', 'text'}:
        return _EDIT_MODE_TEXT
    if text in {'line-range', 'line_range', 'range', 'lines'}:
        return _EDIT_MODE_RANGE
    return text


def _is_zero_like_line_marker(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value == 0
    text = str(value or '').strip()
    if not text:
        return False
    try:
        return int(text) == 0
    except (TypeError, ValueError):
        return False


def _normalize_edit_mode_inputs(
    *,
    mode: Any,
    old_text: str | None,
    new_text: str | None,
    start_line: Any,
    end_line: Any,
    replacement: str | None,
) -> dict[str, Any]:
    normalized_mode = _normalize_edit_mode(mode)
    normalized_start_line = start_line
    normalized_end_line = end_line
    normalized_replacement = replacement
    text_mode_hint = old_text is not None or new_text is not None

    # Some providers/tool adapters auto-fill optional integer/string fields with 0/""
    # even when the model intended text-replace mode. Treat those placeholders as unset.
    if normalized_mode in {'', _EDIT_MODE_TEXT} or text_mode_hint:
        if _is_zero_like_line_marker(normalized_start_line):
            normalized_start_line = None
        if _is_zero_like_line_marker(normalized_end_line):
            normalized_end_line = None
        if normalized_replacement == '':
            normalized_replacement = None

    text_mode = old_text is not None or new_text is not None
    range_mode = (
        normalized_start_line is not None
        or normalized_end_line is not None
        or normalized_replacement is not None
    )
    return {
        'mode': normalized_mode,
        'old_text': old_text,
        'new_text': new_text,
        'start_line': normalized_start_line,
        'end_line': normalized_end_line,
        'replacement': normalized_replacement,
        'text_mode': text_mode,
        'range_mode': range_mode,
    }


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
        settings: FilesystemToolSettings | None = None,
    ) -> None:
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._artifact_store = artifact_store
        self._main_task_service = main_task_service
        self._settings = settings or FilesystemToolSettings()

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

    def _workspace_root(self) -> Path:
        return Path(self._workspace or Path.cwd()).expanduser().resolve()

    def _temp_root(self) -> Path:
        return self._workspace_root() / 'temp'

    def _canonical_temp_root(self, runtime: dict[str, Any] | None = None) -> Path:
        payload = runtime if isinstance(runtime, dict) else {}
        raw = str(payload.get('task_temp_dir') or '').strip()
        if raw:
            try:
                candidate = Path(raw).expanduser().resolve(strict=False)
            except Exception:
                candidate = None
            if candidate is not None and _is_relative_to(candidate, self._workspace_root()):
                return candidate
        return self._temp_root()

    def _externaltools_root(self) -> Path:
        return self._workspace_root() / 'externaltools'

    def _tools_root(self) -> Path:
        return self._workspace_root() / 'tools'

    def _legacy_temp_roots(self) -> list[Path]:
        workspace_root = self._workspace_root()
        return [
            workspace_root / 'tmp',
            workspace_root / '.g3ku' / 'tmp',
        ]

    @staticmethod
    def _system_temp_roots() -> list[Path]:
        candidates: list[Path] = []
        seen: set[str] = set()
        for raw in (
            tempfile.gettempdir(),
            os.environ.get('TMP'),
            os.environ.get('TEMP'),
            os.environ.get('TMPDIR'),
        ):
            text = str(raw or '').strip()
            if not text:
                continue
            try:
                resolved = Path(text).expanduser().resolve()
            except Exception:
                continue
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(resolved)
        return candidates

    @staticmethod
    def _suffix_candidates(path: Path) -> set[str]:
        suffixes = [part.lower() for part in path.suffixes]
        candidates = set(suffixes)
        if len(suffixes) >= 2:
            candidates.add(''.join(suffixes[-2:]))
        if len(suffixes) >= 3:
            candidates.add(''.join(suffixes[-3:]))
        return candidates

    @classmethod
    def _looks_like_managed_artifact(cls, path: Path) -> bool:
        suffix_candidates = cls._suffix_candidates(path)
        managed_suffixes = {
            '.7z',
            '.apk',
            '.appimage',
            '.bin',
            '.bz2',
            '.cache',
            '.cab',
            '.dll',
            '.dmg',
            '.download',
            '.exe',
            '.gz',
            '.iso',
            '.jar',
            '.log',
            '.msi',
            '.partial',
            '.pkg',
            '.rar',
            '.so',
            '.tar',
            '.tar.bz2',
            '.tar.gz',
            '.tar.xz',
            '.temp',
            '.tgz',
            '.tmp',
            '.whl',
            '.xz',
            '.zip',
        }
        if suffix_candidates.intersection(managed_suffixes):
            return True
        lowered_parts = {part.lower() for part in path.parts}
        if lowered_parts.intersection({'node_modules', 'site-packages', '.venv', 'venv', '__pycache__', 'dist', 'build'}):
            return True
        lowered_name = path.name.lower()
        return lowered_name.startswith(('tmp.', 'tmp_', 'tmp-', 'temp.', 'temp_', 'temp-'))

    @staticmethod
    def _is_allowed_tool_registration_path(path: Path, tools_root: Path) -> bool:
        if not _is_relative_to(path, tools_root):
            return False
        relative = path.relative_to(tools_root)
        if len(relative.parts) < 2:
            return False
        if len(relative.parts) == 2 and relative.parts[1] == 'resource.yaml':
            return True
        return relative.parts[1] in {'main', 'toolskills'}

    @classmethod
    def _is_system_temp_path(cls, path: Path) -> bool:
        resolved = path.expanduser().resolve()
        for root in cls._system_temp_roots():
            if _is_relative_to(resolved, root):
                return True
        return False

    def _enforce_workspace_path_policy(self, *, file_path: Path, action: str, runtime: dict[str, Any] | None = None) -> None:
        resolved = file_path.expanduser().resolve()
        workspace_root = self._workspace_root()
        temp_root = self._canonical_temp_root(runtime)
        externaltools_root = self._externaltools_root()
        tools_root = self._tools_root()

        for legacy_root in self._legacy_temp_roots():
            if _is_relative_to(resolved, legacy_root):
                raise PermissionError(
                    f'Path {resolved} is blocked for filesystem.{action}: use {temp_root} for temporary content instead of legacy tmp directories'
                )

        if self._is_system_temp_path(resolved) and not (
            _is_relative_to(resolved, workspace_root) or _is_relative_to(resolved, temp_root)
        ):
            raise PermissionError(
                f'Path {resolved} is blocked for filesystem.{action}: use {temp_root} for temporary content instead of the system temp directory'
            )

        if _is_relative_to(resolved, tools_root) and not self._is_allowed_tool_registration_path(resolved, tools_root):
            raise PermissionError(
                f'Path {resolved} is blocked for filesystem.{action}: tools/ may only contain resource.yaml, main/, and toolskills/ registration content; install real third-party tool payloads under {externaltools_root}'
            )

        if self._looks_like_managed_artifact(resolved) and not (
            _is_relative_to(resolved, temp_root) or _is_relative_to(resolved, externaltools_root)
        ):
            raise PermissionError(
                f'Path {resolved} is blocked for filesystem.{action}: downloads, temporary artifacts, archives, logs, and third-party tool payloads must live under {temp_root} or {externaltools_root}'
            )

    async def write(self, *, path: str, content: str | None, runtime: dict[str, Any]) -> str:
        denied = self._authorize('write', runtime)
        if denied is not None:
            return denied
        try:
            if content is None:
                return 'Error: content is required'
            file_path = _resolve_path(path, self._workspace, self._allowed_dir)
            self._enforce_workspace_path_policy(file_path=file_path, action='write', runtime=runtime)
            existed_before = file_path.exists()
            original = ''
            if existed_before and file_path.is_file():
                original = file_path.read_text(encoding='utf-8')
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding='utf-8')
            validation_result = await self._validate_file(
                file_path=file_path,
                enabled=bool(self._settings.write_validation_enabled),
                timeout_seconds=max(1, int(self._settings.write_validation_timeout_seconds or 20)),
                rollback_on_failure=bool(self._settings.write_validation_rollback_on_failure),
                default_commands=list(self._settings.write_validation_default_commands or []),
                commands_by_ext=dict(self._settings.write_validation_commands_by_ext or {}),
                original_content=original,
                existed_before=existed_before,
                action_label='Write',
            )
            if validation_result is not None:
                return validation_result
            self._record_node_file_change(runtime=runtime, path=file_path, change_type='modified' if existed_before else 'created')
            self._notify_resource_change_paths([file_path], trigger='tool:filesystem.write')
            validated_count = int(self._validation_success_count(
                enabled=bool(self._settings.write_validation_enabled),
                file_path=file_path,
                default_commands=list(self._settings.write_validation_default_commands or []),
                commands_by_ext=dict(self._settings.write_validation_commands_by_ext or {}),
            ) or 0)
            if validated_count > 0:
                return f'Successfully wrote {len(content)} bytes to {file_path} (validated by {validated_count} command(s))'
            return f'Successfully wrote {len(content)} bytes to {file_path}'
        except PermissionError as exc:
            return f'Error: {exc}'
        except (FileNotFoundError, ValueError) as exc:
            return f'Error: {exc}'
        except Exception as exc:
            return f'Error executing filesystem.write: {exc}'

    async def edit(
        self,
        *,
        path: str,
        mode: str | None,
        old_text: str | None,
        new_text: str | None,
        runtime: dict[str, Any],
        start_line: Any = None,
        end_line: Any = None,
        replacement: str | None = None,
    ) -> str:
        denied = self._authorize('edit', runtime)
        if denied is not None:
            return denied
        try:
            normalized = _normalize_edit_mode_inputs(
                mode=mode,
                old_text=old_text,
                new_text=new_text,
                start_line=start_line,
                end_line=end_line,
                replacement=replacement,
            )
            selected_mode = str(normalized.get('mode') or '').strip()
            if selected_mode not in {'', _EDIT_MODE_TEXT, _EDIT_MODE_RANGE}:
                return f'Error: invalid edit mode: {selected_mode}'
            old_text = normalized.get('old_text')
            new_text = normalized.get('new_text')
            start_line = normalized.get('start_line')
            end_line = normalized.get('end_line')
            replacement = normalized.get('replacement')
            text_mode = bool(normalized.get('text_mode'))
            range_mode = bool(normalized.get('range_mode'))

            if selected_mode == _EDIT_MODE_TEXT:
                if range_mode:
                    return _EDIT_MODE_ERROR
                text_mode = True
            elif selected_mode == _EDIT_MODE_RANGE:
                if text_mode:
                    return _EDIT_MODE_ERROR
                range_mode = True
            else:
                if text_mode and range_mode:
                    return _EDIT_MODE_ERROR
                if not text_mode and not range_mode:
                    return _EDIT_MODE_ERROR
                selected_mode = _EDIT_MODE_TEXT if text_mode else _EDIT_MODE_RANGE

            file_path = _resolve_path(path, self._workspace, self._allowed_dir)
            self._enforce_workspace_path_policy(file_path=file_path, action='edit', runtime=runtime)
            if not file_path.exists():
                return f'Error: File not found: {path}'
            if not file_path.is_file():
                return f'Error: Not a file: {path}'
            original = file_path.read_text(encoding='utf-8')
            if selected_mode == _EDIT_MODE_TEXT:
                if old_text is None:
                    return 'Error: old_text is required in text-replace mode'
                if new_text is None:
                    return 'Error: new_text is required in text-replace mode'
                updated_or_error = self._edit_by_text(path=path, content=original, old_text=old_text, new_text=new_text)
            else:
                updated_or_error = self._edit_by_range(path=path, content=original, start_line=start_line, end_line=end_line, replacement=replacement)
            if isinstance(updated_or_error, str) and updated_or_error.startswith(('Error:', 'Warning:')):
                return updated_or_error
            updated = str(updated_or_error)
            file_path.write_text(updated, encoding='utf-8')
            validation_result = await self._validate_file(
                file_path=file_path,
                enabled=bool(self._settings.edit_validation_enabled),
                timeout_seconds=max(1, int(self._settings.edit_validation_timeout_seconds or 20)),
                rollback_on_failure=bool(self._settings.edit_validation_rollback_on_failure),
                default_commands=list(self._settings.edit_validation_default_commands or []),
                commands_by_ext=dict(self._settings.edit_validation_commands_by_ext or {}),
                original_content=original,
                existed_before=True,
                action_label='Edit',
            )
            if validation_result is not None:
                return validation_result
            self._record_node_file_change(runtime=runtime, path=file_path, change_type='modified')
            self._notify_resource_change_paths([file_path], trigger='tool:filesystem.edit')
            validated_count = int(self._validation_success_count(
                enabled=bool(self._settings.edit_validation_enabled),
                file_path=file_path,
                default_commands=list(self._settings.edit_validation_default_commands or []),
                commands_by_ext=dict(self._settings.edit_validation_commands_by_ext or {}),
            ) or 0)
            if validated_count > 0:
                return f'Successfully edited {file_path} (validated by {validated_count} command(s))'
            return f'Successfully edited {file_path}'
        except PermissionError as exc:
            return f'Error: {exc}'
        except (FileNotFoundError, ValueError) as exc:
            return f'Error: {exc}'
        except Exception as exc:
            return f'Error executing filesystem.edit: {exc}'

    def propose_patch(
        self,
        *,
        path: str,
        old_text: str | None,
        new_text: str | None,
        summary: str | None,
        runtime: dict[str, Any],
    ) -> str:
        denied = self._authorize('propose_patch', runtime)
        if denied is not None:
            return denied
        try:
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
                return json.dumps({'success': False, 'error': f'old_text appears multiple times in {path}; provide a more specific match'}, ensure_ascii=False)
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
        except PermissionError as exc:
            return json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False)
        except (FileNotFoundError, ValueError) as exc:
            return json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({'success': False, 'error': f'Error executing filesystem.propose_patch: {exc}'}, ensure_ascii=False)

    def _guard_delete_target(self, *, path: Path) -> None:
        workspace_root = self._workspace_root()
        git_root = workspace_root / '.git'
        resolved = path.expanduser().resolve()
        if resolved == workspace_root:
            raise PermissionError(f'Path {resolved} is blocked for filesystem.delete: refusing to delete the workspace root')
        if resolved == git_root or _is_relative_to(resolved, git_root):
            raise PermissionError(f'Path {resolved} is blocked for filesystem.delete: refusing to delete .git content')

    async def copy(
        self,
        *,
        operations: Any,
        overwrite: Any = False,
        create_parents: Any = True,
        continue_on_error: Any = False,
        runtime: dict[str, Any],
    ) -> str:
        denied = self._authorize('copy', runtime)
        if denied is not None:
            return denied
        try:
            normalized = self._normalize_operations(operations)
            allow_overwrite = bool(overwrite)
            allow_create_parents = bool(create_parents)
            allow_continue = bool(continue_on_error)
            items: list[dict[str, Any]] = []
            changed_paths: list[Path] = []
            for entry in normalized:
                source_path = _resolve_path(entry['source'], self._workspace, self._allowed_dir)
                destination_path = _resolve_path(entry['destination'], self._workspace, self._allowed_dir)
                try:
                    if source_path == destination_path:
                        raise ValueError('source and destination must differ')
                    self._enforce_workspace_path_policy(file_path=destination_path, action='copy', runtime=runtime)
                    item_payload, item_changes = await asyncio.to_thread(
                        self._copy_operation,
                        source_path=source_path,
                        destination_path=destination_path,
                        overwrite=allow_overwrite,
                        create_parents=allow_create_parents,
                    )
                    items.append(item_payload)
                    changed_paths.extend(item_changes)
                    if item_payload['ok']:
                        self._record_node_file_change(
                            runtime=runtime,
                            path=destination_path,
                            change_type=str(item_payload.get('destination_change_type') or 'created'),
                        )
                except Exception as exc:
                    items.append(
                        {
                            'source': str(source_path),
                            'destination': str(destination_path),
                            'ok': False,
                            'status': 'error',
                            'error': str(exc),
                        }
                    )
                    if not allow_continue:
                        break
            self._notify_resource_change_paths(changed_paths, trigger='tool:filesystem.copy')
            return self._batch_result_json(action='copy', requested_total=len(normalized), items=items)
        except PermissionError as exc:
            return self._batch_result_error(action='copy', error=str(exc))
        except (FileNotFoundError, ValueError) as exc:
            return self._batch_result_error(action='copy', error=str(exc))
        except Exception as exc:
            return self._batch_result_error(action='copy', error=f'Error executing filesystem.copy: {exc}')

    async def move(
        self,
        *,
        operations: Any,
        overwrite: Any = False,
        create_parents: Any = True,
        continue_on_error: Any = False,
        runtime: dict[str, Any],
    ) -> str:
        denied = self._authorize('move', runtime)
        if denied is not None:
            return denied
        try:
            normalized = self._normalize_operations(operations)
            allow_overwrite = bool(overwrite)
            allow_create_parents = bool(create_parents)
            allow_continue = bool(continue_on_error)
            items: list[dict[str, Any]] = []
            changed_paths: list[Path] = []
            for entry in normalized:
                source_path = _resolve_path(entry['source'], self._workspace, self._allowed_dir)
                destination_path = _resolve_path(entry['destination'], self._workspace, self._allowed_dir)
                try:
                    if source_path == destination_path:
                        raise ValueError('source and destination must differ')
                    self._enforce_workspace_path_policy(file_path=source_path, action='move', runtime=runtime)
                    self._enforce_workspace_path_policy(file_path=destination_path, action='move', runtime=runtime)
                    item_payload, item_changes = await asyncio.to_thread(
                        self._move_operation,
                        source_path=source_path,
                        destination_path=destination_path,
                        overwrite=allow_overwrite,
                        create_parents=allow_create_parents,
                    )
                    items.append(item_payload)
                    changed_paths.extend(item_changes)
                    if item_payload['ok']:
                        self._record_node_file_change(runtime=runtime, path=source_path, change_type='deleted')
                        self._record_node_file_change(
                            runtime=runtime,
                            path=destination_path,
                            change_type=str(item_payload.get('destination_change_type') or 'created'),
                        )
                except Exception as exc:
                    items.append(
                        {
                            'source': str(source_path),
                            'destination': str(destination_path),
                            'ok': False,
                            'status': 'error',
                            'error': str(exc),
                        }
                    )
                    if not allow_continue:
                        break
            self._notify_resource_change_paths(changed_paths, trigger='tool:filesystem.move')
            return self._batch_result_json(action='move', requested_total=len(normalized), items=items)
        except PermissionError as exc:
            return self._batch_result_error(action='move', error=str(exc))
        except (FileNotFoundError, ValueError) as exc:
            return self._batch_result_error(action='move', error=str(exc))
        except Exception as exc:
            return self._batch_result_error(action='move', error=f'Error executing filesystem.move: {exc}')

    async def delete(
        self,
        *,
        paths: Any,
        recursive: Any = False,
        allow_missing: Any = False,
        continue_on_error: Any = False,
        runtime: dict[str, Any],
    ) -> str:
        denied = self._authorize('delete', runtime)
        if denied is not None:
            return denied
        try:
            normalized_paths = self._normalize_delete_paths(paths)
            allow_recursive = bool(recursive)
            allow_missing_paths = bool(allow_missing)
            allow_continue = bool(continue_on_error)
            items: list[dict[str, Any]] = []
            changed_paths: list[Path] = []
            for raw_path in normalized_paths:
                resolved_path = _resolve_path(raw_path, self._workspace, self._allowed_dir)
                try:
                    self._enforce_workspace_path_policy(file_path=resolved_path, action='delete', runtime=runtime)
                    self._guard_delete_target(path=resolved_path)
                    item_payload, item_changes = await asyncio.to_thread(
                        self._delete_path_entry,
                        path=resolved_path,
                        recursive=allow_recursive,
                        allow_missing=allow_missing_paths,
                    )
                    items.append(item_payload)
                    changed_paths.extend(item_changes)
                    if item_payload['ok'] and str(item_payload.get('status') or '') == 'deleted':
                        self._record_node_file_change(runtime=runtime, path=resolved_path, change_type='deleted')
                except Exception as exc:
                    items.append(
                        {
                            'path': str(resolved_path),
                            'ok': False,
                            'status': 'error',
                            'error': str(exc),
                        }
                    )
                    if not allow_continue:
                        break
            self._notify_resource_change_paths(changed_paths, trigger='tool:filesystem.delete')
            return self._batch_result_json(action='delete', requested_total=len(normalized_paths), items=items)
        except PermissionError as exc:
            return self._batch_result_error(action='delete', error=str(exc))
        except (FileNotFoundError, ValueError) as exc:
            return self._batch_result_error(action='delete', error=str(exc))
        except Exception as exc:
            return self._batch_result_error(action='delete', error=f'Error executing filesystem.delete: {exc}')

    @staticmethod
    def _normalize_operations(operations: Any) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        raw_items = list(operations or []) if isinstance(operations, list) else []
        if not raw_items:
            raise ValueError('operations is required')
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                raise ValueError(f'operations[{index}] must be an object')
            source = str(item.get('source') or '').strip()
            destination = str(item.get('destination') or '').strip()
            if not source:
                raise ValueError(f'operations[{index}].source is required')
            if not destination:
                raise ValueError(f'operations[{index}].destination is required')
            normalized.append({'source': source, 'destination': destination})
        return normalized

    @staticmethod
    def _normalize_delete_paths(paths: Any) -> list[str]:
        raw_items = list(paths or []) if isinstance(paths, list) else []
        if not raw_items:
            raise ValueError('paths is required')
        normalized: list[str] = []
        for index, item in enumerate(raw_items):
            value = str(item or '').strip()
            if not value:
                raise ValueError(f'paths[{index}] is required')
            normalized.append(value)
        return normalized

    @staticmethod
    def _copy_operation(
        *,
        source_path: Path,
        destination_path: Path,
        overwrite: bool,
        create_parents: bool,
    ) -> tuple[dict[str, Any], list[Path]]:
        if not source_path.exists():
            raise FileNotFoundError(f'source not found: {source_path}')
        if source_path.is_dir():
            if destination_path.exists():
                raise ValueError(f'directory destination already exists: {destination_path}')
            if not destination_path.parent.exists():
                if not create_parents:
                    raise FileNotFoundError(f'destination parent does not exist: {destination_path.parent}')
                destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_path, destination_path)
            return ({'source': str(source_path), 'destination': str(destination_path), 'ok': True, 'status': 'copied', 'destination_change_type': 'created'}, [destination_path])
        if not source_path.is_file():
            raise ValueError(f'source is neither file nor directory: {source_path}')
        destination_exists = destination_path.exists()
        if destination_exists and destination_path.is_dir():
            raise ValueError(f'file destination is a directory: {destination_path}')
        if destination_exists and not overwrite:
            raise ValueError(f'destination already exists: {destination_path}')
        if not destination_path.parent.exists():
            if not create_parents:
                raise FileNotFoundError(f'destination parent does not exist: {destination_path.parent}')
            destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        return ({'source': str(source_path), 'destination': str(destination_path), 'ok': True, 'status': 'copied', 'destination_change_type': 'modified' if destination_exists else 'created'}, [destination_path])

    @staticmethod
    def _move_operation(
        *,
        source_path: Path,
        destination_path: Path,
        overwrite: bool,
        create_parents: bool,
    ) -> tuple[dict[str, Any], list[Path]]:
        if not source_path.exists():
            raise FileNotFoundError(f'source not found: {source_path}')
        source_is_dir = source_path.is_dir()
        destination_exists = destination_path.exists()
        if source_is_dir and destination_exists:
            raise ValueError(f'directory destination already exists: {destination_path}')
        if not source_is_dir and destination_exists and destination_path.is_dir():
            raise ValueError(f'file destination is a directory: {destination_path}')
        if not source_is_dir and destination_exists and not overwrite:
            raise ValueError(f'destination already exists: {destination_path}')
        if not destination_path.parent.exists():
            if not create_parents:
                raise FileNotFoundError(f'destination parent does not exist: {destination_path.parent}')
            destination_path.parent.mkdir(parents=True, exist_ok=True)
        if not source_is_dir and destination_exists and overwrite:
            destination_path.unlink()
        shutil.move(str(source_path), str(destination_path))
        return ({'source': str(source_path), 'destination': str(destination_path), 'ok': True, 'status': 'moved', 'destination_change_type': 'modified' if destination_exists else 'created'}, [source_path, destination_path])

    @staticmethod
    def _delete_path_entry(
        *,
        path: Path,
        recursive: bool,
        allow_missing: bool,
    ) -> tuple[dict[str, Any], list[Path]]:
        if not path.exists():
            if allow_missing:
                return ({'path': str(path), 'ok': True, 'status': 'missing'}, [])
            raise FileNotFoundError(f'path not found: {path}')
        if path.is_dir():
            if not recursive:
                raise ValueError(f'directory delete requires recursive=true: {path}')
            shutil.rmtree(path)
            return ({'path': str(path), 'ok': True, 'status': 'deleted'}, [path])
        path.unlink()
        return ({'path': str(path), 'ok': True, 'status': 'deleted'}, [path])

    @staticmethod
    def _edit_by_text(*, path: str, content: str, old_text: str, new_text: str) -> str:
        if old_text not in content:
            return FilesystemTool._not_found_message(old_text, content, path)
        count = content.count(old_text)
        if count > 1:
            return f'Warning: old_text appears {count} times. Please provide more context to make it unique.'
        return content.replace(old_text, new_text, 1)

    @staticmethod
    def _edit_by_range(*, path: str, content: str, start_line: Any, end_line: Any, replacement: str | None) -> str:
        if start_line is None or end_line is None:
            return 'Error: start_line and end_line are required when action=edit in line-range mode'
        try:
            start = int(start_line)
            end = int(end_line)
        except (TypeError, ValueError):
            return 'Error: invalid line range'
        lines = content.splitlines(keepends=True)
        if start < 1 or end < start or end > len(lines):
            return 'Error: invalid line range'
        replacement_text = '' if replacement is None else str(replacement)
        replacement_lines = replacement_text.splitlines(keepends=True)
        if replacement_text and not replacement_lines:
            replacement_lines = [replacement_text]
        updated_lines = lines[: start - 1] + replacement_lines + lines[end:]
        return ''.join(updated_lines)

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

    @staticmethod
    def _batch_result_error(*, action: str, error: str) -> str:
        return json.dumps(
            {
                'ok': False,
                'action': action,
                'partial': False,
                'total': 0,
                'succeeded': 0,
                'failed': 0,
                'items': [],
                'error': str(error or ''),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _batch_result_json(*, action: str, requested_total: int, items: list[dict[str, Any]]) -> str:
        succeeded = sum(1 for item in items if bool(item.get('ok')))
        failed = sum(1 for item in items if not bool(item.get('ok')))
        payload = {
            'ok': failed == 0 and succeeded == requested_total,
            'action': action,
            'partial': succeeded > 0 and failed > 0,
            'total': int(requested_total),
            'succeeded': int(succeeded),
            'failed': int(failed),
            'items': items,
        }
        return json.dumps(payload, ensure_ascii=False)

    async def _validate_file(
        self,
        *,
        file_path: Path,
        enabled: bool,
        timeout_seconds: int,
        rollback_on_failure: bool,
        default_commands: list[str],
        commands_by_ext: dict[str, list[str]],
        original_content: str,
        existed_before: bool,
        action_label: str,
    ) -> str | None:
        if not bool(enabled):
            return None
        commands = self._validation_commands_for(file_path, default_commands=default_commands, commands_by_ext=commands_by_ext)
        if not commands:
            return None
        workspace = Path(self._workspace or file_path.parent).resolve()
        for template in commands:
            command = self._format_validation_command(template=template, file_path=file_path, workspace=workspace)
            result = await self._run_validation_command(command=command, cwd=str(workspace), timeout_seconds=timeout_seconds)
            if not bool(result.get('ok')):
                rollback_applied = False
                if rollback_on_failure:
                    try:
                        if existed_before:
                            file_path.write_text(original_content, encoding='utf-8')
                        elif file_path.exists():
                            file_path.unlink()
                        rollback_applied = True
                    except Exception:
                        rollback_applied = False
                preview = self._validation_error_preview(result)
                failure_suffix = ' after rollback' if rollback_applied else ' without rollback'
                return f"Error: {action_label} validation failed for {file_path}{failure_suffix}. Validation command failed: {command}. {preview}".strip()
        return None

    def _validation_commands_for(
        self,
        file_path: Path,
        *,
        default_commands: list[str],
        commands_by_ext: dict[str, list[str]],
    ) -> list[str]:
        mapping = dict(commands_by_ext or {})
        ext = str(file_path.suffix or '').lower()
        if ext and mapping.get(ext):
            return [str(item) for item in list(mapping.get(ext) or []) if str(item or '').strip()]
        return [str(item) for item in list(default_commands or []) if str(item or '').strip()]

    def _validation_success_count(
        self,
        *,
        enabled: bool,
        file_path: Path,
        default_commands: list[str],
        commands_by_ext: dict[str, list[str]],
    ) -> int:
        if not enabled:
            return 0
        return len(self._validation_commands_for(file_path, default_commands=default_commands, commands_by_ext=commands_by_ext))

    def _format_validation_command(self, *, template: str, file_path: Path, workspace: Path) -> str:
        relative_path = str(file_path.resolve().relative_to(workspace)).replace('\\', '/') if file_path.resolve().is_relative_to(workspace) else file_path.name
        replacements = {
            '{path}': self._quote_shell_value(str(file_path.resolve())),
            '{relative_path}': self._quote_shell_value(relative_path),
            '{workspace}': self._quote_shell_value(str(workspace.resolve())),
        }
        rendered = str(template or '')
        for key, value in replacements.items():
            rendered = rendered.replace(key, value)
        return rendered

    @staticmethod
    def _quote_shell_value(value: str) -> str:
        text = str(value or '')
        if os.name == 'nt':
            return "'" + text.replace("'", "''") + "'"
        escaped = text.replace("'", "'\"'\"'")
        return f"'{escaped}'"

    async def _run_validation_command(self, *, command: str, cwd: str, timeout_seconds: int) -> dict[str, Any]:
        try:
            if os.name == 'nt':
                process = await asyncio.create_subprocess_exec(
                    *self._windows_shell_argv(command),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=enrich_subprocess_env_for_text(os.environ.copy()),
                )
            else:
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=cwd,
                    env=os.environ.copy(),
                )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                return {'ok': False, 'timed_out': True, 'exit_code': None, 'stdout': '', 'stderr': f'Validation command timed out after {timeout_seconds} seconds'}
            return {
                'ok': process.returncode == 0,
                'timed_out': False,
                'exit_code': process.returncode,
                'stdout': decode_subprocess_output(stdout),
                'stderr': decode_subprocess_output(stderr),
            }
        except Exception as exc:
            return {'ok': False, 'timed_out': False, 'exit_code': None, 'stdout': '', 'stderr': str(exc)}

    @staticmethod
    def _validation_error_preview(result: dict[str, Any]) -> str:
        timed_out = bool(result.get('timed_out'))
        exit_code = result.get('exit_code')
        stdout_text = str(result.get('stdout') or '').strip()
        stderr_text = str(result.get('stderr') or '').strip()
        parts: list[str] = []
        if timed_out:
            parts.append('timed out')
        if exit_code not in {None, ''}:
            parts.append(f'exit_code={exit_code}')
        if stderr_text:
            parts.append(f"stderr={stderr_text.splitlines()[0][:240]}")
        elif stdout_text:
            parts.append(f"stdout={stdout_text.splitlines()[0][:240]}")
        return '; '.join(parts)

    @staticmethod
    def _windows_shell_argv(command: str) -> list[str]:
        return [FilesystemTool._windows_powershell_executable(), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', command]

    @staticmethod
    def _windows_powershell_executable() -> str:
        system_root = str(os.environ.get('SystemRoot') or os.environ.get('WINDIR') or '').strip()
        if system_root:
            candidate = Path(system_root) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
            if candidate.exists():
                return str(candidate)
        return 'powershell.exe'

    def _notify_resource_change_paths(self, paths: list[Path], *, trigger: str) -> None:
        normalized_paths = [Path(item).resolve() for item in paths if item is not None]
        if not normalized_paths:
            return
        service = self._main_task_service
        if service is None or not hasattr(service, 'refresh_resource_paths'):
            return
        try:
            service.refresh_resource_paths(normalized_paths, trigger=trigger, session_id='web:shared')
        except Exception:
            return

    def _record_node_file_change(self, *, runtime: dict[str, Any], path: Path, change_type: str) -> None:
        service = self._main_task_service
        if service is None or not hasattr(service, 'record_node_file_change'):
            return
        task_id = _runtime_task_id(runtime)
        node_id = _runtime_node_id(runtime)
        if not task_id or not node_id:
            return
        try:
            service.record_node_file_change(task_id, node_id, path=str(path), change_type=change_type)
        except Exception:
            return


class FilesystemActionTool:
    def __init__(self, delegate: FilesystemTool, *, action: str) -> None:
        self._delegate = delegate
        self._action = str(action or '').strip().lower()

    @property
    def _settings(self) -> FilesystemToolSettings:
        return self._delegate._settings

    @_settings.setter
    def _settings(self, value: FilesystemToolSettings) -> None:
        self._delegate._settings = value

    @property
    def name(self) -> str:
        return {
            'write': 'filesystem_write',
            'edit': 'filesystem_edit',
            'copy': 'filesystem_copy',
            'move': 'filesystem_move',
            'delete': 'filesystem_delete',
            'propose_patch': 'filesystem_propose_patch',
        }.get(self._action, f'filesystem_{self._action}')

    @property
    def description(self) -> str:
        return {
            'write': 'Write file content to disk.',
            'edit': 'Edit one file in text-replace or line-range mode.',
            'copy': 'Copy one or more files or directory trees to new absolute destinations.',
            'move': 'Move one or more files or directory trees to new absolute destinations.',
            'delete': 'Delete one or more files or directory paths.',
            'propose_patch': 'Create a patch artifact without modifying the file.',
        }.get(self._action, self.name)

    @property
    def parameters(self) -> dict[str, Any]:
        if self._action == 'write':
            return {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Absolute file path to write.'},
                    'content': {'type': 'string', 'description': 'Full file content to write.'},
                },
                'required': ['path', 'content'],
            }
        if self._action == 'edit':
            return {
                'type': 'object',
                'properties': {
                    'path': {'type': 'string', 'description': 'Absolute file path to edit.'},
                    'mode': {
                        'type': 'string',
                        'enum': [_EDIT_MODE_TEXT, _EDIT_MODE_RANGE],
                        'description': 'Optional explicit edit mode. Use text_replace with old_text/new_text or line_range with start_line/end_line/replacement.',
                    },
                    'old_text': {'type': 'string', 'description': 'Existing text for text-replace mode.'},
                    'new_text': {'type': 'string', 'description': 'Replacement text for text-replace mode.'},
                    'start_line': {'type': 'integer', 'description': 'Start line for line-range mode.'},
                    'end_line': {'type': 'integer', 'description': 'End line for line-range mode.'},
                    'replacement': {'type': 'string', 'description': 'Replacement text for line-range mode.'},
                },
                'required': ['path'],
            }
        if self._action in {'copy', 'move'}:
            return {
                'type': 'object',
                'properties': {
                    'operations': {
                        'type': 'array',
                        'description': f'{self._action.capitalize()} operations to execute.',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'source': {'type': 'string', 'description': f'Absolute source path to {self._action}.'},
                                'destination': {'type': 'string', 'description': 'Absolute destination path to create.'},
                            },
                            'required': ['source', 'destination'],
                        },
                    },
                    'overwrite': {
                        'type': 'boolean',
                        'description': 'Allow overwriting an existing destination file. Existing destination directories are never allowed.',
                    },
                    'create_parents': {
                        'type': 'boolean',
                        'description': f'Create missing destination parent directories before {self._action}ing.',
                    },
                    'continue_on_error': {
                        'type': 'boolean',
                        'description': f'Continue processing later operations after one {self._action} operation fails.',
                    },
                },
                'required': ['operations'],
            }
        if self._action == 'delete':
            return {
                'type': 'object',
                'properties': {
                    'paths': {
                        'type': 'array',
                        'description': 'Absolute paths to delete.',
                        'items': {'type': 'string', 'description': 'Absolute path to delete.'},
                    },
                    'recursive': {'type': 'boolean', 'description': 'Allow deleting directory paths recursively.'},
                    'allow_missing': {'type': 'boolean', 'description': 'Treat already-missing paths as successful items.'},
                    'continue_on_error': {'type': 'boolean', 'description': 'Continue processing later delete items after one delete fails.'},
                },
                'required': ['paths'],
            }
        return {
            'type': 'object',
            'properties': {
                'path': {'type': 'string', 'description': 'Absolute file path to patch.'},
                'old_text': {'type': 'string', 'description': 'Existing text to replace.'},
                'new_text': {'type': 'string', 'description': 'Proposed replacement text.'},
                'summary': {'type': 'string', 'description': 'Optional patch summary.'},
            },
            'required': ['path', 'old_text', 'new_text'],
        }

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else {}
        fallback_runtime = kwargs.pop('__g3ku_runtime', None)
        if not runtime and isinstance(fallback_runtime, dict):
            runtime = fallback_runtime
        if self._action == 'write':
            return await self._delegate.write(path=str(kwargs.get('path') or ''), content=kwargs.get('content'), runtime=runtime)
        if self._action == 'edit':
            return await self._delegate.edit(
                path=str(kwargs.get('path') or ''),
                mode=kwargs.get('mode'),
                old_text=kwargs.get('old_text'),
                new_text=kwargs.get('new_text'),
                runtime=runtime,
                start_line=kwargs.get('start_line'),
                end_line=kwargs.get('end_line'),
                replacement=kwargs.get('replacement'),
            )
        if self._action == 'copy':
            return await self._delegate.copy(
                operations=kwargs.get('operations'),
                overwrite=kwargs.get('overwrite'),
                create_parents=kwargs.get('create_parents'),
                continue_on_error=kwargs.get('continue_on_error'),
                runtime=runtime,
            )
        if self._action == 'move':
            return await self._delegate.move(
                operations=kwargs.get('operations'),
                overwrite=kwargs.get('overwrite'),
                create_parents=kwargs.get('create_parents'),
                continue_on_error=kwargs.get('continue_on_error'),
                runtime=runtime,
            )
        if self._action == 'delete':
            return await self._delegate.delete(
                paths=kwargs.get('paths'),
                recursive=kwargs.get('recursive'),
                allow_missing=kwargs.get('allow_missing'),
                continue_on_error=kwargs.get('continue_on_error'),
                runtime=runtime,
            )
        if self._action == 'propose_patch':
            return self._delegate.propose_patch(
                path=str(kwargs.get('path') or ''),
                old_text=kwargs.get('old_text'),
                new_text=kwargs.get('new_text'),
                summary=kwargs.get('summary'),
                runtime=runtime,
            )
        return json.dumps({'ok': False, 'error': f'unsupported filesystem split action: {self._action}'}, ensure_ascii=False)


def build_single_purpose_filesystem_tool(runtime, *, action: str) -> FilesystemActionTool:
    settings = runtime_tool_settings(runtime, FilesystemToolSettings)
    service = getattr(runtime.services, 'main_task_service', None)
    artifact_store = getattr(service, 'artifact_store', None) if service is not None else None
    allowed_dir = runtime.workspace if settings.restrict_to_workspace else None
    delegate = FilesystemTool(
        workspace=runtime.workspace,
        allowed_dir=allowed_dir,
        artifact_store=artifact_store,
        main_task_service=service,
        settings=settings,
    )
    return FilesystemActionTool(delegate, action=action)
