from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from g3ku.runtime.context.summarizer import summarize_l0, summarize_l1, summarize_layered_model_first


class ContextCatalogIndexer:
    """Build a global layered catalog for skills/tools using the unified context store."""

    NAMESPACE = ('catalog', 'global')

    def __init__(self, *, memory_manager: Any, service: Any) -> None:
        self._memory_manager = memory_manager
        self._service = service

    def _catalog_summary_model_key(self) -> str | None:
        payload = getattr(self._memory_manager, 'config', None)
        catalog_summary = getattr(payload, 'catalog_summary', None)
        value = str(getattr(catalog_summary, 'model_key', '') or '').strip()
        return value or None

    @staticmethod
    def _catalog_fingerprint_source(*, title: str, description: str, body: str) -> str:
        return json.dumps(
            {
                'title': str(title or '').strip(),
                'description': str(description or '').strip(),
                'content': str(body or '').strip(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    async def sync(
        self,
        *,
        skill_ids: set[str] | None = None,
        tool_ids: set[str] | None = None,
    ) -> dict[str, int]:
        from g3ku.agent.rag_memory import ContextRecordV2

        existing = await self._list_existing()
        seen: set[str] = set()
        created = 0
        updated = 0
        removed = 0
        skill_filter = {str(item).strip() for item in (skill_ids or set()) if str(item).strip()}
        tool_filter = {str(item).strip() for item in (tool_ids or set()) if str(item).strip()}
        subset_mode = bool(skill_filter or tool_filter)

        for skill in list(self._service.list_skill_resources() or []):
            if skill_filter and str(skill.skill_id or '').strip() not in skill_filter:
                continue
            record_id = f'skill:{skill.skill_id}'
            seen.add(record_id)
            display_name = str(getattr(skill, 'display_name', '') or '').strip()
            description = str(getattr(skill, 'description', '') or '').strip()
            path = str(getattr(skill, 'skill_doc_path', '') or '')
            body = ''
            if path and Path(path).exists():
                body = Path(path).read_text(encoding='utf-8')
            text_for_summary = body or description
            fingerprint_source = self._catalog_fingerprint_source(
                title=display_name,
                description=description,
                body=text_for_summary,
            )
            hash_tag = f"hash:{self._memory_manager._stable_text_hash(fingerprint_source)[:12]}"
            tags = ['catalog', 'kind:skill', f'skill:{skill.skill_id}', hash_tag]
            current = existing.get(record_id)
            if current is not None and hash_tag in set(current.tags or []):
                continue
            l0, l1 = await summarize_layered_model_first(
                text_for_summary,
                title=display_name,
                description=description,
                model_key=self._catalog_summary_model_key(),
            )
            record = ContextRecordV2(
                record_id=record_id,
                context_type='skill',
                uri=f'g3ku://skill/{skill.skill_id}',
                parent_uri='g3ku://catalog/skills',
                l0=l0 or summarize_l0(text_for_summary, title=display_name, description=description),
                l1=l1 or summarize_l1(text_for_summary, title=display_name, description=description),
                l2_ref=path or None,
                tags=tags,
                source='catalog',
                confidence=1.0,
                session_key='catalog:global',
                channel='catalog',
                chat_id='global',
            )
            await self._put(record)
            if current is None:
                created += 1
            else:
                updated += 1

        for family in list(self._service.list_tool_resources() or []):
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            if not tool_id:
                continue
            if tool_filter and tool_id not in tool_filter:
                continue
            record_id = f'tool:{tool_id}'
            seen.add(record_id)
            display_name = str(getattr(family, 'display_name', '') or '').strip()
            description = str(getattr(family, 'description', '') or '').strip()
            toolskill = self._service.get_tool_toolskill(tool_id) or {}
            path = str(toolskill.get('path') or '')
            body = str(toolskill.get('content') or '')
            if not body:
                body = description
            fingerprint_source = self._catalog_fingerprint_source(
                title=display_name,
                description=description,
                body=body,
            )
            hash_tag = f"hash:{self._memory_manager._stable_text_hash(fingerprint_source)[:12]}"
            tags = ['catalog', 'kind:tool', f'tool:{tool_id}', hash_tag]
            current = existing.get(record_id)
            if current is not None and hash_tag in set(current.tags or []):
                continue
            l0, l1 = await summarize_layered_model_first(
                body,
                title=display_name,
                description=description,
                model_key=self._catalog_summary_model_key(),
            )
            record = ContextRecordV2(
                record_id=record_id,
                context_type='resource',
                uri=f'g3ku://resource/tool/{tool_id}',
                parent_uri='g3ku://catalog/tools',
                l0=l0 or summarize_l0(body, title=display_name, description=description),
                l1=l1 or summarize_l1(body, title=display_name, description=description),
                l2_ref=path or None,
                tags=tags,
                source='catalog',
                confidence=1.0,
                session_key='catalog:global',
                channel='catalog',
                chat_id='global',
            )
            await self._put(record)
            if current is None:
                created += 1
            else:
                updated += 1

        target_record_ids: set[str]
        if subset_mode:
            target_record_ids = {f'skill:{skill_id}' for skill_id in skill_filter} | {
                f'tool:{tool_id}' for tool_id in tool_filter
            }
        else:
            target_record_ids = set(existing)
        for record_id, record in existing.items():
            if record_id not in target_record_ids:
                continue
            if record_id in seen:
                continue
            await self._delete(record.record_id)
            removed += 1

        return {'created': created, 'updated': updated, 'removed': removed}

    async def _list_existing(self) -> dict[str, Any]:
        records = await self._memory_manager.list_context_records(namespace_prefix=self.NAMESPACE, limit=200000)
        return {record.record_id: record for record in records}

    async def _put(self, record: Any) -> None:
        await self._memory_manager.put_context_record(namespace=self.NAMESPACE, record=record)

    async def _delete(self, record_id: str) -> None:
        await self._memory_manager.delete_context_record(namespace=self.NAMESPACE, record_id=record_id)
