from __future__ import annotations

import asyncio
import base64
import difflib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from g3ku.content import ContentNavigationService
from g3ku.resources.tool_settings import FilesystemToolSettings, runtime_tool_settings

_METADATA_START = '### G3KU_PATCH_METADATA ###'
_DIFF_START = '### G3KU_PATCH_DIFF ###'


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


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


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
        self._content_store = getattr(main_task_service, 'content_store', None) if main_task_service is not None else None
        self._settings = settings or FilesystemToolSettings()

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
            if operation == 'describe':
                return self._describe(path)
            if operation == 'search':
                return self._search(path, kwargs.get('query'), kwargs.get('limit'), kwargs.get('before'), kwargs.get('after'))
            if operation == 'open':
                return self._open(path, kwargs.get('start_line'), kwargs.get('end_line'), kwargs.get('around_line'), kwargs.get('window'))
            if operation == 'head':
                return self._head(path, kwargs.get('lines'))
            if operation == 'tail':
                return self._tail(path, kwargs.get('lines'))
            if operation == 'list':
                return self._list(path)
            if operation == 'write':
                return await self._write(path, content)
            if operation == 'edit':
                return await self._edit(path, old_text, new_text, runtime=runtime, **kwargs)
            if operation == 'delete':
                return self._delete(path)
            if operation == 'propose_patch':
                return self._propose_patch(path, old_text, new_text, summary, runtime)
            return f'Error: Unsupported filesystem action: {operation}'
        except PermissionError as exc:
            return f'Error: {exc}'
        except (FileNotFoundError, ValueError) as exc:
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

    def _workspace_root(self) -> Path:
        return Path(self._workspace or Path.cwd()).expanduser().resolve()

    def _temp_root(self) -> Path:
        return self._workspace_root() / 'temp'

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

    def _enforce_workspace_path_policy(self, *, file_path: Path, action: str) -> None:
        resolved = file_path.expanduser().resolve()
        temp_root = self._temp_root()
        externaltools_root = self._externaltools_root()
        tools_root = self._tools_root()

        for legacy_root in self._legacy_temp_roots():
            if _is_relative_to(resolved, legacy_root):
                raise PermissionError(
                    f'Path {resolved} is blocked for filesystem.{action}: use {temp_root} for temporary content instead of legacy tmp directories'
                )

        if self._is_system_temp_path(resolved) and not _is_relative_to(resolved, temp_root):
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

    def _describe(self, path: str) -> str:
        _raise_if_content_ref_path(path)
        return json.dumps(self._navigator().describe(path=path), ensure_ascii=False)

    def _search(self, path: str, query: Any, limit: Any, before: Any, after: Any) -> str:
        resolved = _resolve_path(path, self._workspace, self._allowed_dir)
        needle = str(query or '').strip()
        if not needle:
            return json.dumps({'ok': False, 'error': 'query is required'}, ensure_ascii=False)
        max_hits = max(1, min(int(limit or 10), 50))
        before_count = max(0, min(int(before or 2), 10))
        after_count = max(0, min(int(after or 2), 10))
        if not resolved.exists():
            missing_kind = 'Directory' if str(path).endswith(('\\', '/')) or not Path(path).suffix else 'File'
            raise FileNotFoundError(f'{missing_kind} not found: {path}')
        if resolved.is_dir():
            hits, overflow = self._search_directory(resolved, query=needle, limit=max_hits)
            if overflow:
                return json.dumps(
                    self._search_refine_payload(
                        query=needle,
                        cap=max_hits,
                        scope_type='directory',
                        path=str(resolved),
                    ),
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    'ok': True,
                    'path': str(resolved),
                    'query': needle,
                    'scope_type': 'directory',
                    'hits': hits,
                    'count': len(hits),
                    'overflow': False,
                    'requires_refine': False,
                    'cap': max_hits,
                    'overflow_lower_bound': None,
                    'message': '',
                    'suggestions': [],
                },
                ensure_ascii=False,
            )
        if not resolved.is_file():
            raise ValueError(f'path is neither a file nor a directory: {path}')
        result = self._navigator().search(
            path=str(resolved),
            query=needle,
            limit=max_hits,
            before=before_count,
            after=after_count,
        )
        result['scope_type'] = 'file'
        result['path'] = str(resolved)
        for hit in list(result.get('hits') or []):
            if isinstance(hit, dict):
                hit.setdefault('path', str(resolved))
        return json.dumps(result, ensure_ascii=False)

    def _open(self, path: str, start_line: Any, end_line: Any, around_line: Any, window: Any) -> str:
        _raise_if_content_ref_path(path)
        return json.dumps(
            self._navigator().open(
                path=path,
                start_line=int(start_line) if start_line is not None else None,
                end_line=int(end_line) if end_line is not None else None,
                around_line=int(around_line) if around_line is not None else None,
                window=int(window) if window is not None else None,
            ),
            ensure_ascii=False,
        )

    def _head(self, path: str, lines: Any) -> str:
        _raise_if_content_ref_path(path)
        return json.dumps(self._navigator().head(path=path, lines=int(lines or 80)), ensure_ascii=False)

    def _tail(self, path: str, lines: Any) -> str:
        _raise_if_content_ref_path(path)
        return json.dumps(self._navigator().tail(path=path, lines=int(lines or 80)), ensure_ascii=False)

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

    async def _write(self, path: str, content: str | None) -> str:
        if content is None:
            return 'Error: content is required when action=write'
        file_path = _resolve_path(path, self._workspace, self._allowed_dir)
        self._enforce_workspace_path_policy(file_path=file_path, action='write')
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
        self._notify_resource_change(file_path, trigger='tool:filesystem.write')
        validated_count = int(self._validation_success_count(
            enabled=bool(self._settings.write_validation_enabled),
            file_path=file_path,
            default_commands=list(self._settings.write_validation_default_commands or []),
            commands_by_ext=dict(self._settings.write_validation_commands_by_ext or {}),
        ) or 0)
        if validated_count > 0:
            return f'Successfully wrote {len(content)} bytes to {file_path} (validated by {validated_count} command(s))'
        return f'Successfully wrote {len(content)} bytes to {file_path}'

    async def _edit(
        self,
        path: str,
        old_text: str | None,
        new_text: str | None,
        *,
        runtime: dict[str, Any],
        start_line: Any = None,
        end_line: Any = None,
        replacement: str | None = None,
        **_: Any,
    ) -> str:
        text_mode = old_text is not None or new_text is not None
        range_mode = start_line is not None or end_line is not None or replacement is not None
        if text_mode and range_mode:
            return 'Error: edit requires exactly one mode: text-replace or line-range'
        if not text_mode and not range_mode:
            return 'Error: edit requires exactly one mode: text-replace or line-range'
        file_path = _resolve_path(path, self._workspace, self._allowed_dir)
        self._enforce_workspace_path_policy(file_path=file_path, action='edit')
        if not file_path.exists():
            return f'Error: File not found: {path}'
        if not file_path.is_file():
            return f'Error: Not a file: {path}'
        original = file_path.read_text(encoding='utf-8')
        if text_mode:
            if old_text is None:
                return 'Error: old_text is required when action=edit in text-replace mode'
            if new_text is None:
                return 'Error: new_text is required when action=edit in text-replace mode'
            updated_or_error = self._edit_by_text(path=path, content=original, old_text=old_text, new_text=new_text)
        else:
            updated_or_error = self._edit_by_range(
                path=path,
                content=original,
                start_line=start_line,
                end_line=end_line,
                replacement=replacement,
            )
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
        self._notify_resource_change(file_path, trigger='tool:filesystem.edit')
        validated_count = int(self._validation_success_count(
            enabled=bool(self._settings.edit_validation_enabled),
            file_path=file_path,
            default_commands=list(self._settings.edit_validation_default_commands or []),
            commands_by_ext=dict(self._settings.edit_validation_commands_by_ext or {}),
        ) or 0)
        if validated_count > 0:
            return f'Successfully edited {file_path} (validated by {validated_count} command(s))'
        return f'Successfully edited {file_path}'

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

    def _delete(self, path: str) -> str:
        file_path = _resolve_path(path, self._workspace, self._allowed_dir)
        self._enforce_workspace_path_policy(file_path=file_path, action='delete')
        if not file_path.exists():
            return f'Error: File not found: {path}'
        if not file_path.is_file():
            return f'Error: Not a file: {path}'
        file_path.unlink()
        self._notify_resource_change(file_path, trigger='tool:filesystem.delete')
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

    def _navigator(self) -> ContentNavigationService:
        artifact_store = self._artifact_store
        if artifact_store is None and self._main_task_service is not None:
            artifact_store = getattr(self._main_task_service, 'artifact_store', None)
        return ContentNavigationService(
            workspace=self._workspace or Path.cwd(),
            allowed_dir=self._allowed_dir,
            artifact_store=artifact_store,
            artifact_lookup=self._main_task_service or artifact_store,
        )

    @staticmethod
    def _search_directory(path: Path, *, query: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(query), re.IGNORECASE)

        hits: list[dict[str, Any]] = []
        total_matches = 0
        for file_path in sorted(path.rglob('*')):
            if not file_path.is_file():
                continue
            try:
                with file_path.open('r', encoding='utf-8') as handle:
                    for line_number, line in enumerate(handle, start=1):
                        if '\x00' in line:
                            raise UnicodeDecodeError('utf-8', b'\x00', 0, 1, 'binary content')
                        if not pattern.search(line):
                            continue
                        total_matches += 1
                        if total_matches > limit:
                            return hits, True
                        hits.append(
                            {
                                'path': str(file_path.resolve()),
                                'line': line_number,
                                'preview': line.strip()[:240],
                            }
                        )
            except (OSError, UnicodeDecodeError):
                continue
        return hits, False

    def _search_refine_payload(self, *, query: str, cap: int, scope_type: str, path: str) -> dict[str, Any]:
        suggestions = [
            'Use a more specific symbol, function name, or field name.',
            'Narrow the path to a smaller file or directory before searching again.',
        ]
        if scope_type == 'file':
            suggestions.append('Open a local excerpt first, then search within that smaller context.')
        else:
            suggestions.append('List or describe the directory first, then search a smaller subtree.')
            suggestions.append('Limit the query to a filename pattern or extension before retrying.')
        return {
            'ok': True,
            'path': path,
            'query': query,
            'scope_type': scope_type,
            'hits': [],
            'count': 0,
            'overflow': True,
            'requires_refine': True,
            'cap': cap,
            'overflow_lower_bound': cap + 1,
            'message': f'Search matched more than {cap} results. Refine the query before retrying.',
            'suggestions': suggestions,
        }

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
            result = await self._run_validation_command(
                command=command,
                cwd=str(workspace),
                timeout_seconds=timeout_seconds,
            )
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
                    env=os.environ.copy(),
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
                return {
                    'ok': False,
                    'timed_out': True,
                    'exit_code': None,
                    'stdout': '',
                    'stderr': f'Validation command timed out after {timeout_seconds} seconds',
                }
            return {
                'ok': process.returncode == 0,
                'timed_out': False,
                'exit_code': process.returncode,
                'stdout': stdout.decode('utf-8', errors='replace') if stdout else '',
                'stderr': stderr.decode('utf-8', errors='replace') if stderr else '',
            }
        except Exception as exc:
            return {
                'ok': False,
                'timed_out': False,
                'exit_code': None,
                'stdout': '',
                'stderr': str(exc),
            }

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
        return [
            FilesystemTool._windows_powershell_executable(),
            '-NoProfile',
            '-NonInteractive',
            '-ExecutionPolicy',
            'Bypass',
            '-Command',
            command,
        ]

    @staticmethod
    def _windows_powershell_executable() -> str:
        system_root = str(os.environ.get('SystemRoot') or os.environ.get('WINDIR') or '').strip()
        if system_root:
            candidate = Path(system_root) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
            if candidate.exists():
                return str(candidate)
        return 'powershell.exe'

    def _notify_resource_change(self, path: Path, *, trigger: str) -> None:
        service = self._main_task_service
        if service is None or not hasattr(service, 'refresh_resource_paths'):
            return
        session_id = 'web:shared'
        try:
            service.refresh_resource_paths([path], trigger=trigger, session_id=session_id)
        except Exception:
            return


def build(runtime):
    settings = runtime_tool_settings(runtime, FilesystemToolSettings, tool_name='filesystem')
    service = getattr(runtime.services, 'main_task_service', None)
    artifact_store = getattr(service, 'artifact_store', None) if service is not None else None
    allowed_dir = runtime.workspace if settings.restrict_to_workspace else None
    return FilesystemTool(
        workspace=runtime.workspace,
        allowed_dir=allowed_dir,
        artifact_store=artifact_store,
        main_task_service=service,
        settings=settings,
    )
