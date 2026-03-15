from __future__ import annotations

from pathlib import Path
from typing import Any

from g3ku.runtime.context.summarizer import summarize_l0, summarize_l1, summarize_layered_model_first


class ContextCatalogIndexer:
    """Build a global layered catalog for skills/tools using the unified context store."""

    NAMESPACE = ('catalog', 'global')

    def __init__(self, *, memory_manager: Any, service: Any) -> None:
        self._memory_manager = memory_manager
        self._service = service

    async def sync(self) -> dict[str, int]:
        from g3ku.agent.rag_memory import ContextRecordV2

        existing = await self._list_existing()
        seen: set[str] = set()
        created = 0
        updated = 0

        for skill in list(self._service.list_skill_resources() or []):
            record_id = f'skill:{skill.skill_id}'
            seen.add(record_id)
            path = str(getattr(skill, 'skill_doc_path', '') or '')
            body = ''
            if path and Path(path).exists():
                body = Path(path).read_text(encoding='utf-8')
            text_for_summary = body or str(getattr(skill, 'description', '') or '')
            hash_tag = f"hash:{self._memory_manager._stable_text_hash(text_for_summary)[:12]}"
            tags = ['catalog', 'kind:skill', f'skill:{skill.skill_id}', hash_tag]
            current = existing.get(record_id)
            if current is not None and hash_tag in set(current.tags or []):
                continue
            l0, l1 = await summarize_layered_model_first(
                text_for_summary,
                title=getattr(skill, 'display_name', ''),
                description=getattr(skill, 'description', ''),
            )
            record = ContextRecordV2(
                record_id=record_id,
                context_type='skill',
                uri=f'g3ku://skill/{skill.skill_id}',
                parent_uri='g3ku://catalog/skills',
                l0=l0 or summarize_l0(text_for_summary, title=getattr(skill, 'display_name', ''), description=getattr(skill, 'description', '')),
                l1=l1 or summarize_l1(text_for_summary, title=getattr(skill, 'display_name', ''), description=getattr(skill, 'description', '')),
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
            record_id = f'tool:{tool_id}'
            seen.add(record_id)
            toolskill = self._service.get_tool_toolskill(tool_id) or {}
            path = str(toolskill.get('path') or '')
            body = str(toolskill.get('content') or '')
            if not body:
                body = str(getattr(family, 'description', '') or '')
            hash_tag = f"hash:{self._memory_manager._stable_text_hash(body)[:12]}"
            tags = ['catalog', 'kind:tool', f'tool:{tool_id}', hash_tag]
            current = existing.get(record_id)
            if current is not None and hash_tag in set(current.tags or []):
                continue
            l0, l1 = await summarize_layered_model_first(
                body,
                title=getattr(family, 'display_name', ''),
                description=getattr(family, 'description', ''),
            )
            record = ContextRecordV2(
                record_id=record_id,
                context_type='resource',
                uri=f'g3ku://resource/tool/{tool_id}',
                parent_uri='g3ku://catalog/tools',
                l0=l0 or summarize_l0(body, title=getattr(family, 'display_name', ''), description=getattr(family, 'description', '')),
                l1=l1 or summarize_l1(body, title=getattr(family, 'display_name', ''), description=getattr(family, 'description', '')),
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

        removed = 0
        for record_id, record in existing.items():
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
