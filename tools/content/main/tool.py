from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from g3ku.content import ContentNavigationService, parse_content_envelope
from g3ku.resources.tool_settings import ContentToolSettings, load_tool_settings_from_manifest, runtime_tool_settings


class ContentTool:
    def __init__(self, *, workspace: Path, content_store: ContentNavigationService | None = None) -> None:
        self._content_store = content_store or ContentNavigationService(workspace=workspace)

    @staticmethod
    def _normalize_ref(ref: str | None) -> str:
        envelope = parse_content_envelope(ref)
        if envelope is not None:
            return str(envelope.ref or '').strip()
        return str(ref or '').strip()

    def _canonical_artifact_ref(self, ref: str | None) -> str:
        normalized = self._normalize_ref(ref)
        if not normalized.startswith('artifact:'):
            return normalized
        try:
            payload = self._content_store.describe(ref=normalized, view='canonical')
        except Exception:
            return normalized
        resolved_ref = self._normalize_ref(payload.get('resolved_ref'))
        return resolved_ref if resolved_ref.startswith('artifact:') else normalized

    def _guard_ref_access(self, *, runtime: dict[str, Any] | None, ref: str | None) -> str | None:
        payload = runtime if isinstance(runtime, dict) else {}
        if not bool(payload.get('enforce_content_ref_allowlist')):
            return None
        requested_ref = self._normalize_ref(ref)
        if not requested_ref.startswith('artifact:'):
            return None
        allowed_refs = sorted(
            {
                self._normalize_ref(item)
                for item in list(payload.get('allowed_content_refs') or [])
                if self._normalize_ref(item).startswith('artifact:')
            }
        )
        if requested_ref in allowed_refs:
            return None
        requested_canonical = self._canonical_artifact_ref(requested_ref)
        allowed_canonical_refs = {self._canonical_artifact_ref(item) for item in allowed_refs}
        if requested_canonical in allowed_canonical_refs:
            return None
        return json.dumps(
            {
                'ok': False,
                'error': 'artifact ref not allowed in this context',
                'requested_ref': requested_ref,
                'allowed_refs': allowed_refs,
            },
            ensure_ascii=False,
        )

    def _execute_operation(
        self,
        *,
        operation: str,
        ref: str | None = None,
        path: str | None = None,
        query: str | None = None,
        view: str | None = None,
        limit: int | None = None,
        before: int | None = None,
        after: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        around_line: int | None = None,
        window: int | None = None,
        lines: int | None = None,
    ) -> dict[str, Any]:
        if operation == "describe":
            return self._content_store.describe(ref=ref, path=path, view=str(view or "canonical"))
        if operation == "search":
            return self._content_store.search(
                ref=ref,
                path=path,
                query=str(query or ""),
                view=str(view or "canonical"),
                limit=int(limit or 10),
                before=int(before or 2),
                after=int(after or 2),
            )
        if operation == "open":
            return self._content_store.open(
                ref=ref,
                path=path,
                view=str(view or "canonical"),
                start_line=int(start_line) if start_line is not None else None,
                end_line=int(end_line) if end_line is not None else None,
                around_line=int(around_line) if around_line is not None else None,
                window=int(window) if window is not None else None,
            )
        if operation == "head":
            return self._content_store.head(ref=ref, path=path, view=str(view or "canonical"), lines=int(lines or 80))
        if operation == "tail":
            return self._content_store.tail(ref=ref, path=path, view=str(view or "canonical"), lines=int(lines or 80))
        raise ValueError(f"Unsupported content action: {operation}")

    def _attempt_single_target_operation(
        self,
        *,
        operation: str,
        target: str,
        runtime: dict[str, Any] | None,
        ref: str | None = None,
        path: str | None = None,
        query: str | None = None,
        view: str | None = None,
        limit: int | None = None,
        before: int | None = None,
        after: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        around_line: int | None = None,
        window: int | None = None,
        lines: int | None = None,
    ) -> dict[str, Any]:
        try:
            if target == "ref":
                blocked = self._guard_ref_access(runtime=runtime, ref=ref)
                if blocked is not None:
                    payload = json.loads(blocked)
                else:
                    payload = self._execute_operation(
                        operation=operation,
                        ref=ref,
                        query=query,
                        view=view,
                        limit=limit,
                        before=before,
                        after=after,
                        start_line=start_line,
                        end_line=end_line,
                        around_line=around_line,
                        window=window,
                        lines=lines,
                    )
            elif target == "path":
                payload = self._execute_operation(
                    operation=operation,
                    path=path,
                    query=query,
                    view=view,
                    limit=limit,
                    before=before,
                    after=after,
                    start_line=start_line,
                    end_line=end_line,
                    around_line=around_line,
                    window=window,
                    lines=lines,
                )
            else:
                raise ValueError(f"unsupported content target: {target}")
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)}
        normalized = dict(payload)
        normalized.setdefault("ok", bool(normalized.get("ref") or normalized.get("hits") or normalized.get("excerpt") or normalized.get("content")))
        normalized["target"] = target
        if ref is not None:
            normalized.setdefault("input_ref", str(ref))
        if path is not None:
            normalized.setdefault("input_path", str(path))
        return normalized

    @staticmethod
    def _combine_target_results(
        *,
        operation: str,
        ref: str,
        path: str,
        ref_result: dict[str, Any],
        path_result: dict[str, Any],
    ) -> dict[str, Any]:
        targets = {"ref": ref_result, "path": path_result}
        successes = [name for name, payload in targets.items() if payload.get("ok") is True]
        failures = [name for name, payload in targets.items() if payload.get("ok") is not True]
        payload: dict[str, Any] = {
            "ok": bool(successes),
            "combined": True,
            "operation": operation,
            "requested_ref": str(ref or "").strip(),
            "requested_path": str(path or "").strip(),
            "targets": targets,
            "success_count": len(successes),
            "failure_count": len(failures),
            "partial": bool(successes) and bool(failures),
        }
        if not successes:
            payload["error"] = "all requested content targets failed"
        return payload

    async def execute(
        self,
        action: str,
        ref: str | None = None,
        path: str | None = None,
        query: str | None = None,
        view: str | None = None,
        limit: int | None = None,
        before: int | None = None,
        after: int | None = None,
        start_line: int | None = None,
        end_line: int | None = None,
        around_line: int | None = None,
        window: int | None = None,
        lines: int | None = None,
        __g3ku_runtime: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = __g3ku_runtime if isinstance(__g3ku_runtime, dict) else None
        if runtime is None:
            fallback_runtime = kwargs.get('__g3ku_runtime')
            runtime = fallback_runtime if isinstance(fallback_runtime, dict) else None
        operation = str(action or "").strip().lower()
        normalized_ref = str(ref or "").strip()
        normalized_path = str(path or "").strip()
        try:
            if normalized_ref and normalized_path and operation in {"search", "open"}:
                ref_result = self._attempt_single_target_operation(
                    operation=operation,
                    target="ref",
                    runtime=runtime,
                    ref=normalized_ref,
                    view=view,
                    query=query,
                    limit=limit,
                    before=before,
                    after=after,
                    start_line=start_line,
                    end_line=end_line,
                    around_line=around_line,
                    window=window,
                    lines=lines,
                )
                path_result = self._attempt_single_target_operation(
                    operation=operation,
                    target="path",
                    runtime=runtime,
                    path=normalized_path,
                    view=view,
                    query=query,
                    limit=limit,
                    before=before,
                    after=after,
                    start_line=start_line,
                    end_line=end_line,
                    around_line=around_line,
                    window=window,
                    lines=lines,
                )
                return json.dumps(
                    self._combine_target_results(
                        operation=operation,
                        ref=normalized_ref,
                        path=normalized_path,
                        ref_result=ref_result,
                        path_result=path_result,
                    ),
                    ensure_ascii=False,
                )
            blocked = self._guard_ref_access(runtime=runtime, ref=ref)
            if blocked is not None:
                return blocked
            return json.dumps(
                self._execute_operation(
                    operation=operation,
                    ref=ref,
                    path=path,
                    query=query,
                    view=view,
                    limit=limit,
                    before=before,
                    after=after,
                    start_line=start_line,
                    end_line=end_line,
                    around_line=around_line,
                    window=window,
                    lines=lines,
                ),
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


class ContentActionTool:
    def __init__(self, delegate: ContentTool, *, action: str, fixed_kwargs: dict[str, Any] | None = None) -> None:
        self._delegate = delegate
        self._action = str(action or '').strip().lower()
        self._fixed_kwargs = dict(fixed_kwargs or {})

    async def execute(self, __g3ku_runtime: dict[str, Any] | None = None, **kwargs: Any) -> str:
        call_kwargs = dict(self._fixed_kwargs)
        call_kwargs.update(kwargs)
        call_kwargs.pop('action', None)
        return await self._delegate.execute(
            action=self._action,
            __g3ku_runtime=__g3ku_runtime,
            **call_kwargs,
        )


def build_content_tool(runtime, *, tool_name: str | None = None) -> ContentTool:
    resolved_tool_name = str(tool_name or getattr(getattr(runtime, 'resource_descriptor', None), 'name', '') or 'content')
    descriptor_name = str(getattr(getattr(runtime, 'resource_descriptor', None), 'name', '') or '').strip()
    if resolved_tool_name and resolved_tool_name != descriptor_name:
        settings = load_tool_settings_from_manifest(runtime.workspace, resolved_tool_name, ContentToolSettings)
    else:
        settings = runtime_tool_settings(runtime, ContentToolSettings, tool_name=resolved_tool_name)
    service = getattr(runtime.services, "main_task_service", None)
    shared_store = getattr(service, "content_store", None) if service is not None else None
    artifact_store = getattr(shared_store, "_artifact_store", None)
    artifact_lookup = getattr(shared_store, "_artifact_lookup", None)
    if artifact_store is None and service is not None:
        artifact_store = getattr(service, "artifact_store", None)
    if artifact_lookup is None and service is not None:
        artifact_lookup = getattr(service, "store", None) or artifact_store
    content_store = ContentNavigationService(
        workspace=runtime.workspace,
        allowed_dir=runtime.workspace if settings.restrict_to_workspace else None,
        artifact_store=artifact_store,
        artifact_lookup=artifact_lookup,
    )
    return ContentTool(workspace=runtime.workspace, content_store=content_store)


def build_single_purpose_content_tool(runtime, *, action: str, fixed_kwargs: dict[str, Any] | None = None) -> ContentActionTool:
    return ContentActionTool(
        build_content_tool(runtime, tool_name='content'),
        action=action,
        fixed_kwargs=fixed_kwargs,
    )


def build(runtime):
    return build_content_tool(runtime, tool_name='content')
