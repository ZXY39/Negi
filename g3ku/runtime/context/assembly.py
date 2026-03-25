from __future__ import annotations

from pathlib import Path
from typing import Any

from g3ku.runtime.context.summarizer import estimate_tokens, score_query, truncate_by_tokens
from g3ku.runtime.context.types import ContextAssemblyResult
from g3ku.runtime.core_tools import resolve_core_tool_targets
from g3ku.runtime.web_ceo_sessions import (
    DEFAULT_FRONTDOOR_RAW_TAIL_TURNS,
    build_frontdoor_compact_history_message,
    extract_frontdoor_recent_history,
    resolve_frontdoor_context,
)


class ContextAssemblyService:
    """Budget-aware CEO context assembly with visibility-preserving selection."""

    EXTENSION_TOOL_HINTS: dict[str, tuple[str, ...]] = {
        'cron': ('remind', 'schedule', 'cron', 'timer', 'recurring'),
        'load_skill_context': ('skill', 'workflow', 'procedure', 'steps', 'context', 'details'),
        'load_tool_context': ('tool', 'api', 'parameters', 'usage', 'context', 'details'),
        'filesystem': ('file', 'path', 'read', 'write', 'edit', 'list', 'open'),
        'exec': ('shell', 'command', 'bash', 'powershell', 'terminal', 'run'),
        'model_config': ('model', 'provider', 'config', 'token', 'temperature'),
    }
    SKILL_RETRIEVAL_HINTS: tuple[str, ...] = ('skill', 'workflow', 'steps', 'skill.md', 'load_skill_context')
    RESOURCE_RETRIEVAL_HINTS: tuple[str, ...] = ('tool', 'api', 'usage', 'install', 'update', 'load_tool_context')
    TARGETED_RETRIEVAL_SCORE_THRESHOLD: float = 2.0
    RESERVED_INTERNAL_TOOLS: tuple[str, ...] = ("wait_tool_execution", "stop_tool_execution")

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
        archive_top_k = max(0, int(getattr(assembly_cfg, 'archive_summary_top_k', 2) or 2))
        archive_budget = max(64, int(getattr(assembly_cfg, 'archive_summary_max_tokens', 320) or 320))
        inventory_top_k = max(1, int(getattr(assembly_cfg, 'skill_inventory_top_k', 8) or 8))
        inventory_budget = max(64, int(getattr(assembly_cfg, 'skill_inventory_max_tokens', 480) or 480))
        extension_top_k = max(0, int(getattr(assembly_cfg, 'extension_tool_top_k', 6) or 6))
        core_tools = {
            str(name).strip()
            for name in list(getattr(assembly_cfg, 'core_tools', []) or [])
            if str(name).strip()
        }

        if main_service is not None and memory_manager is not None and getattr(self._loop, '_use_rag_memory', lambda: False)():
            try:
                await memory_manager.sync_catalog(main_service)
            except Exception:
                pass

        visible_skills = list(exposure.get('skills') or [])
        visible_families = list(exposure.get('tool_families') or [])
        selected_skills, skill_trace = self._select_skills(
            query_text=query_text,
            visible_skills=visible_skills,
            top_k=inventory_top_k,
            token_budget=inventory_budget,
        )
        retrieval_scope = self._plan_retrieval_scope(
            query_text=query_text,
            visible_skills=visible_skills,
            visible_families=visible_families,
        )
        prompt_skills = list(selected_skills)
        system_prompt = self._prompt_builder.build(skills=prompt_skills)

        external_tools_block, external_trace = self._build_external_tool_block(
            query_text=query_text,
            visible_families=visible_families,
            top_k=8,
        )
        if external_tools_block:
            system_prompt = f"{system_prompt}\n\n{external_tools_block}"

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
                    channel=getattr(session, '_memory_channel', getattr(session, '_channel', 'cli')),
                    chat_id=getattr(session, '_memory_chat_id', getattr(session, '_chat_id', session.state.session_key)),
                    search_context_types=retrieval_scope['search_context_types'],
                    allowed_context_types=retrieval_scope['allowed_context_types'],
                    allowed_resource_record_ids=retrieval_scope['allowed_resource_record_ids'],
                    allowed_skill_record_ids=retrieval_scope['allowed_skill_record_ids'],
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
            visible_families=visible_families,
            core_tools=core_tools,
            extension_top_k=extension_top_k,
        )

        frontdoor_context: dict[str, Any] = {}
        frontdoor_context_source = ''
        frontdoor_summary_message: dict[str, Any] | None = None
        frontdoor_summary_tokens = 0
        recent_history = []
        if persisted_session is not None:
            frontdoor_context, frontdoor_context_source = resolve_frontdoor_context(
                persisted_session,
                raw_tail_turns=DEFAULT_FRONTDOOR_RAW_TAIL_TURNS,
            )
            frontdoor_summary_message = build_frontdoor_compact_history_message(frontdoor_context)
            if frontdoor_summary_message is not None:
                recent_history.append(frontdoor_summary_message)
                frontdoor_summary_tokens = estimate_tokens(str(frontdoor_summary_message.get('content') or ''))
            recent_history.extend(
                extract_frontdoor_recent_history(
                    persisted_session,
                    raw_tail_turns=int(frontdoor_context.get('raw_tail_turns', DEFAULT_FRONTDOOR_RAW_TAIL_TURNS) or DEFAULT_FRONTDOOR_RAW_TAIL_TURNS),
                )
            )

        trace = {
            'query': query_text,
            'selected_skills': skill_trace,
            'selected_tools': tool_trace,
            'external_tools': external_trace,
            'archive_summaries': archive_trace,
            'frontdoor_context': {
                'source': frontdoor_context_source,
                'summary_turn_count': int(frontdoor_context.get('summary_turn_count', 0) or 0),
                'raw_tail_turns': int(frontdoor_context.get('raw_tail_turns', 0) or 0),
                'has_summary': bool(str(frontdoor_context.get('summary_text') or '').strip()),
            },
            'retrieval_scope': {
                'search_context_types': list(retrieval_scope['search_context_types']),
                'allowed_context_types': list(retrieval_scope['allowed_context_types']),
                'allowed_resource_record_ids': list(retrieval_scope['allowed_resource_record_ids']),
                'allowed_skill_record_ids': list(retrieval_scope['allowed_skill_record_ids']),
                'targeted_skill_ids': list(retrieval_scope['targeted_skill_ids']),
                'targeted_tool_ids': list(retrieval_scope['targeted_tool_ids']),
                'deduped_skill_ids': list(retrieval_scope['deduped_skill_ids']),
                'deduped_tool_ids': list(retrieval_scope['deduped_tool_ids']),
            },
            'recent_history_count': len(recent_history),
            'tokens': {
                'system_prompt': estimate_tokens(system_prompt),
                'skill_inventory': sum(int(item.get('tokens', 0)) for item in skill_trace),
                'external_tools': sum(int(item.get('tokens', 0)) for item in external_trace),
                'archive_summaries': sum(int(item.get('tokens', 0)) for item in archive_trace),
                'frontdoor_summary': frontdoor_summary_tokens,
                'retrieved_context': retrieval_tokens,
            },
        }
        if memory_manager is not None and hasattr(memory_manager, 'write_context_assembly_trace'):
            try:
                await memory_manager.write_context_assembly_trace(
                    session_key=session.state.session_key,
                    channel=getattr(session, '_memory_channel', getattr(session, '_channel', 'cli')),
                    chat_id=getattr(session, '_memory_chat_id', getattr(session, '_chat_id', session.state.session_key)),
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

    def _plan_retrieval_scope(
        self,
        *,
        query_text: str,
        visible_skills: list[Any],
        visible_families: list[Any],
    ) -> dict[str, list[str]]:
        query_lower = str(query_text or '').strip().lower()
        targeted_skill_ids: list[str] = []
        targeted_tool_ids: list[str] = []

        for item in list(visible_skills or []):
            skill_id = str(getattr(item, 'skill_id', '') or '').strip()
            if not skill_id:
                continue
            display_name = str(getattr(item, 'display_name', '') or '').strip()
            score = score_query(
                query_text,
                getattr(item, 'description', ''),
            )
            if skill_id.lower() in query_lower or (display_name and display_name.lower() in query_lower) or score >= self.TARGETED_RETRIEVAL_SCORE_THRESHOLD:
                targeted_skill_ids.append(skill_id)

        for family in list(visible_families or []):
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            if not tool_id:
                continue
            display_name = str(getattr(family, 'display_name', '') or '').strip()
            executor_names: list[str] = []
            for action in list(getattr(family, 'actions', []) or []):
                executor_names.extend(
                    str(name or '').strip()
                    for name in list(getattr(action, 'executor_names', []) or [])
                    if str(name or '').strip()
                )
            score = score_query(
                query_text,
                getattr(family, 'description', ''),
                ' '.join(executor_names),
            )
            if tool_id.lower() in query_lower or (display_name and display_name.lower() in query_lower) or score >= self.TARGETED_RETRIEVAL_SCORE_THRESHOLD:
                targeted_tool_ids.append(tool_id)

        if not targeted_skill_ids and any(hint.lower() in query_lower for hint in self.SKILL_RETRIEVAL_HINTS):
            targeted_skill_ids = [
                str(getattr(item, 'skill_id', '') or '').strip()
                for item in list(visible_skills or [])
                if str(getattr(item, 'skill_id', '') or '').strip()
            ][:3]
        if not targeted_tool_ids and any(hint.lower() in query_lower for hint in self.RESOURCE_RETRIEVAL_HINTS):
            targeted_tool_ids = [
                str(getattr(item, 'tool_id', '') or '').strip()
                for item in list(visible_families or [])
                if str(getattr(item, 'tool_id', '') or '').strip()
            ][:3]

        targeted_skill_ids = sorted(set(targeted_skill_ids))
        targeted_tool_ids = sorted(set(targeted_tool_ids))

        search_context_types = ['memory']
        if targeted_skill_ids:
            search_context_types.append('skill')
        if targeted_tool_ids:
            search_context_types.append('resource')

        return {
            'search_context_types': search_context_types,
            'allowed_context_types': list(search_context_types),
            'allowed_resource_record_ids': [f'tool:{item}' for item in targeted_tool_ids] if targeted_tool_ids else [],
            'allowed_skill_record_ids': [f'skill:{item}' for item in targeted_skill_ids] if targeted_skill_ids else [],
            'targeted_skill_ids': targeted_skill_ids,
            'targeted_tool_ids': targeted_tool_ids,
            'deduped_skill_ids': [],
            'deduped_tool_ids': [],
        }

    def _select_skills(
        self,
        *,
        query_text: str,
        visible_skills: list[Any],
        top_k: int,
        token_budget: int,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
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
                    'score': score_query(
                        query_text,
                        getattr(item, 'skill_id', ''),
                        getattr(item, 'display_name', ''),
                        getattr(item, 'description', ''),
                    ),
                    'tokens': line_tokens,
                }
            )
        if not selected:
            selected = ranked[:top_k]
            trace = [{'skill_id': getattr(item, 'skill_id', ''), 'score': 0.0, 'tokens': 0} for item in selected]
        return selected, trace

    def _build_external_tool_block(
        self,
        *,
        query_text: str,
        visible_families: list[Any],
        top_k: int,
        exclude_tool_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        ranked: list[tuple[float, str, Any]] = []
        excluded = {str(item or '').strip() for item in list(exclude_tool_ids or []) if str(item or '').strip()}
        for family in visible_families:
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            install_dir = str(getattr(family, 'install_dir', '') or '').strip()
            callable_flag = bool(getattr(family, 'callable', True))
            available_flag = bool(getattr(family, 'available', True))
            if not tool_id:
                continue
            if tool_id in excluded and available_flag:
                continue
            if callable_flag and available_flag:
                continue
            if not install_dir and not callable_flag:
                continue
            score = score_query(
                query_text,
                tool_id,
                getattr(family, 'display_name', ''),
                getattr(family, 'description', ''),
                install_dir,
            )
            ranked.append((score, tool_id, family))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        if not ranked:
            return '', []

        lines = ['## Tool Resources That Require `load_tool_context`']
        trace: list[dict[str, Any]] = []
        for score, tool_id, family in ranked[: max(1, top_k)]:
            display_name = str(getattr(family, 'display_name', '') or tool_id).strip() or tool_id
            description = str(getattr(family, 'description', '') or '').strip()
            install_dir = str(getattr(family, 'install_dir', '') or '').strip()
            callable_flag = bool(getattr(family, 'callable', True))
            available_flag = bool(getattr(family, 'available', True))
            issue_summary = self._tool_family_issue_summary(family)
            if not callable_flag:
                state_text = f"Install dir: `{install_dir}`"
                if not available_flag:
                    state_text = f"{state_text}. Status: unavailable"
                    if issue_summary:
                        state_text = f"{state_text} ({issue_summary})"
                line = (
                    f"- `{tool_id}` ({display_name}): {description or 'Registered external tool resource.'} {state_text}. "
                    f"For install, update, troubleshooting, or usage guidance, call `load_tool_context(tool_id=\"{tool_id}\")`."
                )
            else:
                state_text = "Status: unavailable"
                if issue_summary:
                    state_text = f"{state_text} ({issue_summary})"
                line = (
                    f"- `{tool_id}` ({display_name}): {description or 'Tool guidance resource.'} {state_text}. "
                    f"It will not appear in the callable function tool list until fixed. "
                    f"For repair steps or usage guidance, call `load_tool_context(tool_id=\"{tool_id}\")`."
                )
            line_tokens = estimate_tokens(line)
            lines.append(line)
            trace.append(
                {
                    'tool_id': tool_id,
                    'score': score,
                    'install_dir': install_dir,
                    'callable': callable_flag,
                    'available': available_flag,
                    'tokens': line_tokens,
                }
            )
        return '\n'.join(lines), trace

    @staticmethod
    def _tool_family_issue_summary(family: Any) -> str:
        metadata = getattr(family, 'metadata', {}) or {}
        warnings = [str(item or '').strip() for item in list(metadata.get('warnings') or []) if str(item or '').strip()]
        errors = [str(item or '').strip() for item in list(metadata.get('errors') or []) if str(item or '').strip()]
        issues = errors + warnings
        return '; '.join(issues[:2])

    def _build_archive_block(
        self,
        *,
        query_text: str,
        session: Any | None,
        top_k: int,
        token_budget: int,
    ) -> tuple[str, list[dict[str, Any]]]:
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
            scored.append(
                (
                    score,
                    created_at,
                    {
                        'summary_uri': summary_uri,
                        'content': content,
                        'archive_id': segment.get('archive_id', ''),
                    },
                )
            )
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
            trace.append(
                {
                    'archive_id': payload['archive_id'],
                    'summary_uri': payload['summary_uri'],
                    'score': score,
                    'tokens': line_tokens,
                }
            )
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
        raw_core_entries = {str(name).strip() for name in list(core_tools or []) if str(name).strip()}
        core_resolution = resolve_core_tool_targets(core_tools, visible_families)
        reserved = [name for name in visible_names if name in self.RESERVED_INTERNAL_TOOLS and name in visible_set]
        reserved_set = set(reserved)
        selected = {
            name
            for name in visible_set
            if (name in core_resolution.executor_names or name in raw_core_entries) and name not in reserved_set
        }
        if extension_top_k <= 0:
            ordered = reserved + [name for name in visible_names if name in selected]
            return ordered, {'reserved': reserved, 'core': sorted(selected), 'extension': []}

        ext_scored: list[tuple[float, list[str], str]] = []
        for family in visible_families:
            family_names = []
            for action in list(getattr(family, 'actions', []) or []):
                for executor_name in list(getattr(action, 'executor_names', []) or []):
                    name = str(executor_name or '').strip()
                    if (
                        name
                        and name in visible_set
                        and name not in core_resolution.executor_names
                        and name not in raw_core_entries
                        and name not in reserved_set
                    ):
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
                if name in reserved_set:
                    continue
                picked_extension.append(name)
                if len(picked_extension) >= extension_top_k:
                    break

        ordered = (
            reserved
            + [name for name in visible_names if name in selected]
            + [name for name in visible_names if name in picked_extension]
        )
        return ordered, {'reserved': reserved, 'core': sorted(selected), 'extension': picked_extension}
