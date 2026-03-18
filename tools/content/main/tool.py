from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from g3ku.content import ContentNavigationService, parse_content_envelope


class ContentTool:
    def __init__(self, *, workspace: Path, content_store: ContentNavigationService | None = None) -> None:
        self._content_store = content_store or ContentNavigationService(workspace=workspace)

    @staticmethod
    def _normalize_ref(ref: str | None) -> str:
        envelope = parse_content_envelope(ref)
        if envelope is not None:
            return str(envelope.ref or '').strip()
        return str(ref or '').strip()

    @classmethod
    def _guard_ref_access(cls, *, runtime: dict[str, Any] | None, ref: str | None) -> str | None:
        payload = runtime if isinstance(runtime, dict) else {}
        if not bool(payload.get('enforce_content_ref_allowlist')):
            return None
        requested_ref = cls._normalize_ref(ref)
        if not requested_ref.startswith('artifact:'):
            return None
        allowed_refs = sorted(
            {
                cls._normalize_ref(item)
                for item in list(payload.get('allowed_content_refs') or [])
                if cls._normalize_ref(item).startswith('artifact:')
            }
        )
        if requested_ref in allowed_refs:
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

    async def execute(
        self,
        action: str,
        ref: str | None = None,
        path: str | None = None,
        query: str | None = None,
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
        blocked = self._guard_ref_access(runtime=runtime, ref=ref)
        if blocked is not None:
            return blocked
        if operation == "describe":
            return json.dumps(self._content_store.describe(ref=ref, path=path), ensure_ascii=False)
        if operation == "search":
            return json.dumps(
                self._content_store.search(
                    ref=ref,
                    path=path,
                    query=str(query or ""),
                    limit=int(limit or 10),
                    before=int(before or 2),
                    after=int(after or 2),
                ),
                ensure_ascii=False,
            )
        if operation == "open":
            return json.dumps(
                self._content_store.open(
                    ref=ref,
                    path=path,
                    start_line=int(start_line) if start_line is not None else None,
                    end_line=int(end_line) if end_line is not None else None,
                    around_line=int(around_line) if around_line is not None else None,
                    window=int(window) if window is not None else None,
                ),
                ensure_ascii=False,
            )
        if operation == "head":
            return json.dumps(self._content_store.head(ref=ref, path=path, lines=int(lines or 80)), ensure_ascii=False)
        if operation == "tail":
            return json.dumps(self._content_store.tail(ref=ref, path=path, lines=int(lines or 80)), ensure_ascii=False)
        return json.dumps({"ok": False, "error": f"Unsupported content action: {operation}"}, ensure_ascii=False)


def build(runtime):
    service = getattr(runtime.services, "main_task_service", None)
    content_store = getattr(service, "content_store", None) if service is not None else None
    return ContentTool(workspace=runtime.workspace, content_store=content_store)
