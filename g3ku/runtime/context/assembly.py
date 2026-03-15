from __future__ import annotations

from pathlib import Path
from typing import Any

from g3ku.runtime.context.summarizer import estimate_tokens, score_query, truncate_by_tokens
from g3ku.runtime.context.types import ContextAssemblyResult


class ContextAssemblyService:
    """Budget-aware CEO context assembly with visibility-preserving selection."""

    EXTENSION_TOOL_HINTS: dict[str, tuple[str, ...]] = {
        'cron': ('提醒', '定时', 'schedule', 'cron', 'remind'),
        'load_skill_context': ('skill', '技能', '流程', 'workflow', '上下文', '详细'),
        'load_tool_context': ('tool', '工具', '参数', 'api', '调用', '详情'),
        'filesystem': ('文件', '路径', 'path', 'read', 'write', 'edit', 'list', 'open'),
        'exec': ('shell', 'command', 'bash', 'powershell', '终端', '执行命令'),
        'model_config': ('model', 'provider', 'config', 'token', 'temperature', '模型', '配置'),
    }

    def __init__(self, *, loop, prompt_builder) -> None:
        self._loop = loop
        self._prompt_builder = prompt_builder

    async def build_for_ceo(
        self,
        *,
        session: Any,
        query_text: str,
        exposure: dict[str, Any],
        persisted_session: Any | None,
    ) -> ContextAssemblyResult:
        main_service = getattr(self._loop, 'main_task_service', None)
        memory_manager = getattr(self._loop, 'memory_manager', None)
        assembly_cfg = getattr(getattr(self._loop, '_memory_runtime_settings', None), 'assembly', None)
        recent_limit = max(1, int(getattr(assembly_cfg, 'recent_messages_limit', 24) or 24))
        fallback_limit = max(recent_limit, int(getattr(self._loop, 'memory_window', 100) or 100))
        archive_top_k = max(0, int(getattr(assembly_cfg, 'archive_summary_top_k', 2) or 2))
        archive_budget = max(64, int(getattr(assembly_cfg, 'archive_summary_max_tokens', 320) or 320))
        inventory_top_k = max(1, int(getattr(assembly_cfg, 'skill_inventory_top_k', 8) or 8))
        inventory_budget = max(64, int(getattr(assembly_cfg, 'skill_inventory_max_tokens', 480) or 480))
        extension_top_k = max(0, int(getattr(assembly_cfg, 'extension_tool_top_k', 6) or 6))
        core_tools = {str(name).strip() for name in list(getattr(assembly_cfg, 'core_tools', []) or []) if str(name).strip()}

        if main_service is not None and memory_manager is not None and getattr(self._loop, '_use_rag_memory', lambda: False)():
            try:
                await memory_manager.sync_catalog(main_service)
            except Exception:
                pass

        visible_skills = list(exposure.get('skills') or [])
        selected_skills, skill_trace = self._select_skills(
            query_text=query_text,
            visible_skills=visible_skills,
            top_k=inventory_top_k,
            token_budget=inventory_budget,
        )
        system_prompt = self._prompt_builder.build(skills=selected_skills)

        archive_block, archive_trace = self._build_archive_block(
            query_text=query_text,
            session=persisted_session,
            top_k=archive_top_k,
            token_budget=archive_budget,
        )
        if archive_block:
            system_prompt = f"{system_prompt}\n\n{archive_block}"

        retrieved_memory = ''
        retrieval_tokens = 0
        if memory_manager is not None and getattr(self._loop, '_use_rag_memory', lambda: False)() and query_text:
            try:
                retrieved_memory = await memory_manager.retrieve_block(
                    query=query_text,
                    session_key=session.state.session_key,
                    channel=getattr(session, '_channel', 'cli'),
                    chat_id=getattr(session, '_chat_id', session.state.session_key),
                )
            except Exception:
                retrieved_memory = ''
        if retrieved_memory:
            retrieval_tokens = estimate_tokens(retrieved_memory)
            if '# Retrieved Context' in retrieved_memory:
                system_prompt = f"{system_prompt}\n\n{retrieved_memory}"
            else:
                system_prompt = f"{system_prompt}\n\n# Retrieved Context\n\n{retrieved_memory}"

        selected_tool_names, tool_trace = self._select_tools(
            query_text=query_text,
            visible_names=list(exposure.get('tool_names') or []),
            visible_families=list(exposure.get('tool_families') or []),
            core_tools=core_tools,
            extension_top_k=extension_top_k,
        )

        recent_history = []
        if persisted_session is not None:
            has_compact_context = bool(archive_block or retrieved_memory)
            recent_history = persisted_session.get_history(max_messages=recent_limit if has_compact_context else fallback_limit)

        trace = {
            'query': query_text,
            'selected_skills': skill_trace,
            'selected_tools': tool_trace,
            'archive_summaries': archive_trace,
            'recent_history_count': len(recent_history),
            'tokens': {
                'system_prompt': estimate_tokens(system_prompt),
                'skill_inventory': sum(int(item.get('tokens', 0)) for item in skill_trace),
                'archive_summaries': sum(int(item.get('tokens', 0)) for item in archive_trace),
                'retrieved_context': retrieval_tokens,
            },
        }
        if memory_manager is not None and hasattr(memory_manager, 'write_context_assembly_trace'):
            try:
                await memory_manager.write_context_assembly_trace(
                    session_key=session.state.session_key,
                    channel=getattr(session, '_channel', 'cli'),
                    chat_id=getattr(session, '_chat_id', session.state.session_key),
                    payload=trace,
                )
            except Exception:
                pass
        return ContextAssemblyResult(
            system_prompt=system_prompt,
            recent_history=recent_history,
            tool_names=selected_tool_names,
            trace=trace,
        )

    def _select_skills(self, *, query_text: str, visible_skills: list[Any], top_k: int, token_budget: int) -> tuple[list[Any], list[dict[str, Any]]]:
        ranked = sorted(
            visible_skills,
            key=lambda item: (
                score_query(
                    query_text,
                    getattr(item, 'skill_id', ''),
                    getattr(item, 'display_name', ''),
                    getattr(item, 'description', ''),
                ),
                getattr(item, 'skill_id', ''),
            ),
            reverse=True,
        )
        selected: list[Any] = []
        trace: list[dict[str, Any]] = []
        used_tokens = 0
        for item in ranked[: max(top_k * 2, top_k)]:
            line = f"- {getattr(item, 'skill_id', '')}: {getattr(item, 'description', '') or getattr(item, 'display_name', '')}"
            line_tokens = estimate_tokens(line)
            if len(selected) >= top_k:
                break
            if used_tokens + line_tokens > token_budget:
                continue
            selected.append(item)
            used_tokens += line_tokens
            trace.append(
                {
                    'skill_id': getattr(item, 'skill_id', ''),
                    'score': score_query(query_text, getattr(item, 'skill_id', ''), getattr(item, 'display_name', ''), getattr(item, 'description', '')),
                    'tokens': line_tokens,
                }
            )
        if not selected:
            selected = ranked[:top_k]
            trace = [{'skill_id': getattr(item, 'skill_id', ''), 'score': 0.0, 'tokens': 0} for item in selected]
        return selected, trace

    def _build_archive_block(self, *, query_text: str, session: Any | None, top_k: int, token_budget: int) -> tuple[str, list[dict[str, Any]]]:
        if session is None or top_k <= 0:
            return '', []
        segments = list(getattr(session, 'archive_segments', []) or [])
        if not segments:
            return '', []
        scored: list[tuple[float, str, dict[str, Any]]] = []
        for segment in segments:
            summary_uri = str(segment.get('summary_uri') or '').strip()
            if not summary_uri:
                continue
            path = Path(summary_uri)
            if not path.exists():
                continue
            try:
                content = path.read_text(encoding='utf-8').strip()
            except Exception:
                continue
            if not content:
                continue
            score = score_query(query_text, content, segment.get('reason', ''), segment.get('archive_id', ''))
            created_at = str(segment.get('created_at') or '')
            scored.append((score, created_at, {'summary_uri': summary_uri, 'content': content, 'archive_id': segment.get('archive_id', '')}))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        lines = ['## Related Session Archives']
        trace: list[dict[str, Any]] = []
        used_tokens = estimate_tokens(lines[0])
        for score, _created_at, payload in scored[:top_k]:
            text = truncate_by_tokens(payload['content'], max(32, token_budget // max(top_k, 1)))
            line = f"- [{payload['archive_id']}] {text}"
            line_tokens = estimate_tokens(line)
            if used_tokens + line_tokens > token_budget:
                continue
            lines.append(line)
            used_tokens += line_tokens
            trace.append({'archive_id': payload['archive_id'], 'summary_uri': payload['summary_uri'], 'score': score, 'tokens': line_tokens})
        return ('\n'.join(lines) if len(lines) > 1 else ''), trace

    def _select_tools(
        self,
        *,
        query_text: str,
        visible_names: list[str],
        visible_families: list[Any],
        core_tools: set[str],
        extension_top_k: int,
    ) -> tuple[list[str], dict[str, Any]]:
        visible_set = {str(name).strip() for name in visible_names if str(name).strip()}
        if not visible_set:
            return [], {'core': [], 'extension': []}
        selected = {name for name in visible_set if name in core_tools}
        ext_scored: list[tuple[float, list[str], str]] = []
        for family in visible_families:
            family_names = []
            for action in list(getattr(family, 'actions', []) or []):
                for executor_name in list(getattr(action, 'executor_names', []) or []):
                    name = str(executor_name or '').strip()
                    if name and name in visible_set and name not in core_tools:
                        family_names.append(name)
            family_names = sorted(set(family_names))
            if not family_names:
                continue
            score = score_query(
                query_text,
                getattr(family, 'tool_id', ''),
                getattr(family, 'display_name', ''),
                getattr(family, 'description', ''),
                ' '.join(family_names),
            )
            for name in family_names:
                for hint in self.EXTENSION_TOOL_HINTS.get(name, ()):
                    if hint and hint.lower() in str(query_text or '').lower():
                        score += 5.0
            ext_scored.append((score, family_names, str(getattr(family, 'tool_id', '') or '')))
        ext_scored.sort(key=lambda item: (item[0], item[2]), reverse=True)
        picked_extension: list[str] = []
        for score, names, tool_id in ext_scored:
            if len(picked_extension) >= extension_top_k:
                break
            for name in names:
                if name in selected or name in picked_extension:
                    continue
                picked_extension.append(name)
                if len(picked_extension) >= extension_top_k:
                    break
        if not picked_extension:
            for name in sorted(visible_set - selected):
                picked_extension.append(name)
                if len(picked_extension) >= extension_top_k:
                    break
        ordered = [name for name in visible_names if name in selected] + [name for name in visible_names if name in picked_extension]
        return ordered, {'core': sorted(selected), 'extension': picked_extension}
