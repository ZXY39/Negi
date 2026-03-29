from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from g3ku.runtime.context.semantic_scope import plan_retrieval_scope, semantic_catalog_rankings
from g3ku.runtime.context.summarizer import estimate_tokens, score_query, truncate_by_tokens
from g3ku.runtime.context.types import ContextAssemblyResult
from g3ku.runtime.core_tools import resolve_core_tool_targets
from g3ku.runtime.web_ceo_sessions import (
    DEFAULT_FRONTDOOR_RAW_TAIL_TURNS,
    build_active_tasks_message,
    build_last_task_memory,
    build_frontdoor_compact_history_message,
    build_task_memory_message,
    extract_frontdoor_recent_history,
    resolve_frontdoor_context,
)


class ContextAssemblyService:
    """Budget-aware CEO context assembly with visibility-preserving selection."""

    MEMORY_WRITE_STRONG_TERMS: tuple[str, ...] = (
        '记住',
        '请记住',
        '不要再',
        '别再',
        '默认',
        '长期按这个来',
        'please remember',
        'from now on',
        'going forward',
        'default to',
        "don't use",
        'never use',
    )
    MEMORY_WRITE_FUTURE_RE = re.compile(r'(以后|今后).{0,24}(都|默认|不要|别再|按这个|用)')
    MEMORY_WRITE_REMEMBER_RE = re.compile(r'\bremember\b')
    MEMORY_WRITE_REMEMBER_EXCLUSION_RE = re.compile(r'\b(?:what|do)\s+you\s+remember\b')
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

    @classmethod
    def _detect_memory_write_intent(cls, query_text: str) -> list[str]:
        text = str(query_text or '').strip()
        if not text:
            return []
        lower = text.lower()
        matched: list[str] = []
        for term in cls.MEMORY_WRITE_STRONG_TERMS:
            if term in {'please remember', 'remember'}:
                continue
            haystack = lower if term.isascii() else text
            if term in haystack:
                matched.append(term)
        if 'please remember' in lower:
            matched.append('please remember')
        if (
            cls.MEMORY_WRITE_REMEMBER_RE.search(lower)
            and not cls.MEMORY_WRITE_REMEMBER_EXCLUSION_RE.search(lower)
            and 'please remember' not in lower
        ):
            matched.append('remember')
        if cls.MEMORY_WRITE_FUTURE_RE.search(text):
            if '以后' in text:
                matched.append('以后')
            if '今后' in text:
                matched.append('今后')
        deduped: list[str] = []
        for item in matched:
            if item not in deduped:
                deduped.append(item)
        return deduped

    @staticmethod
    def _memory_write_hint_block(matched_terms: list[str]) -> str:
        terms = ', '.join(matched_terms)
        return '\n'.join(
            [
                '## Long-Term Memory Write Hint',
                f'- The current user turn likely requests durable memory updates ({terms}).',
                '- If this is a stable identity, preference, constraint, default, avoidance rule, workflow rule, or durable project fact, call `memory_write` before replying.',
                '- Do not save temporary task state, guesses, or unconfirmed inferences as permanent memory.',
            ]
        )

    @staticmethod
    def _retrieved_memory_resolution_hint_block() -> str:
        return '\n'.join(
            [
                '## Retrieved Memory Resolution Hint',
                '- The retrieved memory below already contains previously confirmed user defaults or preferences relevant to this turn.',
                '- If the user is asking what the default is, restate the retrieved default directly.',
                '- Do not invent a new default, propose alternatives, or replace the retrieved default with general best practices unless the user explicitly asks to change the rule.',
            ]
        )

    @staticmethod
    def _extract_authoritative_retrieved_memory_fact(retrieved_memory: str) -> str:
        text = str(retrieved_memory or '')
        if not text:
            return ''
        current_label = ''
        for raw_line in text.splitlines():
            line = str(raw_line or '').strip()
            if not line:
                continue
            if line.startswith('- ['):
                marker = '] '
                idx = line.find(marker)
                if idx >= 0:
                    current_label = line[3:idx].strip()
                    content = line[idx + len(marker) :].strip()
                    if content and current_label and ':' not in current_label:
                        return content
                continue
            if line.startswith('L2:') or line.startswith('L1:'):
                content = line[3:].strip()
                if content:
                    return content
        return ''

    @classmethod
    def _authoritative_retrieved_memory_fact_block(cls, retrieved_memory: str) -> str:
        fact = cls._extract_authoritative_retrieved_memory_fact(retrieved_memory)
        if not fact:
            return ''
        return '\n'.join(
            [
                '## Authoritative Retrieved Default',
                '- A previously confirmed user default relevant to this turn was found in long-term memory.',
                f'- Default to follow right now: {fact}',
                '- Answer by restating this default directly before adding any optional clarifications.',
            ]
        )

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

        if main_service is not None and memory_manager is not None:
            try:
                await memory_manager.sync_catalog(main_service)
            except Exception:
                pass

        memory_write_terms = self._detect_memory_write_intent(query_text)

        visible_skills = list(exposure.get('skills') or [])
        visible_families = list(exposure.get('tool_families') or [])
        semantic_frontdoor = await semantic_catalog_rankings(
            memory_manager=memory_manager,
            query_text=query_text,
            visible_skills=visible_skills,
            visible_families=visible_families,
            skill_limit=max(inventory_top_k * 4, len(visible_skills), inventory_top_k, 8),
            tool_limit=max(extension_top_k * 4, len(visible_families), extension_top_k, 8),
        )
        selected_skills, skill_trace = self._select_skills(
            visible_skills=visible_skills,
            top_k=inventory_top_k,
            token_budget=inventory_budget,
            ranked_skill_ids=semantic_frontdoor['skill_ids'],
        )
        retrieval_scope = plan_retrieval_scope(
            visible_skills=visible_skills,
            visible_families=visible_families,
            semantic_frontdoor=semantic_frontdoor,
        )
        if memory_write_terms:
            retrieval_scope = {
                **retrieval_scope,
                'search_context_types': ['memory'],
                'allowed_context_types': ['memory'],
                'allowed_resource_record_ids': [],
                'allowed_skill_record_ids': [],
            }
        prompt_skills = list(selected_skills)
        system_prompt = self._prompt_builder.build(skills=prompt_skills)

        external_tools_block, external_trace = self._build_external_tool_block(
            visible_families=visible_families,
            visible_tool_names=list(exposure.get('tool_names') or []),
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
        if memory_manager is not None and query_text:
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
            retrieved_block = retrieved_memory if '# Retrieved Context' in retrieved_memory else f"# Retrieved Context\n\n{retrieved_memory}"
            if memory_write_terms:
                authority_block = self._authoritative_retrieved_memory_fact_block(retrieved_memory)
                hint_parts = [self._retrieved_memory_resolution_hint_block()]
                if authority_block:
                    hint_parts.append(authority_block)
                retrieved_block = f"{'\n\n'.join(hint_parts)}\n\n{retrieved_block}"
            system_prompt = f"{system_prompt}\n\n{retrieved_block}"

        memory_write_visible = 'memory_write' in {str(name or '').strip() for name in list(exposure.get('tool_names') or [])}
        if memory_write_terms and memory_write_visible:
            system_prompt = f"{system_prompt}\n\n{self._memory_write_hint_block(memory_write_terms)}"

        selected_tool_names, tool_trace = self._select_tools(
            query_text=query_text,
            visible_names=list(exposure.get('tool_names') or []),
            visible_families=visible_families,
            core_tools=core_tools,
            extension_top_k=extension_top_k,
            ranked_tool_ids=semantic_frontdoor['tool_ids'],
        )

        frontdoor_context: dict[str, Any] = {}
        frontdoor_context_source = ''
        frontdoor_summary_message: dict[str, Any] | None = None
        frontdoor_summary_tokens = 0
        recent_history = []
        active_task_message: dict[str, Any] | None = None
        active_task_count = 0
        list_active_task_snapshots = getattr(main_service, 'list_active_task_snapshots_for_session', None)
        if callable(list_active_task_snapshots):
            active_tasks = list_active_task_snapshots(getattr(session.state, 'session_key', ''), limit=3)
            active_task_count = len(list(active_tasks or []))
            active_task_message = build_active_tasks_message(active_tasks, limit=3)
            if active_task_message is not None:
                recent_history.append(active_task_message)
        if persisted_session is not None:
            frontdoor_context, frontdoor_context_source = resolve_frontdoor_context(
                persisted_session,
                raw_tail_turns=DEFAULT_FRONTDOOR_RAW_TAIL_TURNS,
            )
            frontdoor_summary_message = build_frontdoor_compact_history_message(frontdoor_context)
            if frontdoor_summary_message is not None:
                recent_history.append(frontdoor_summary_message)
                frontdoor_summary_tokens = estimate_tokens(str(frontdoor_summary_message.get('content') or ''))
            task_memory_message = build_task_memory_message(
                getattr(persisted_session, 'metadata', {}).get('last_task_memory')
            )
            if task_memory_message is None:
                task_memory_message = build_task_memory_message(build_last_task_memory(persisted_session))
            if task_memory_message is not None:
                recent_history.append(task_memory_message)
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
            'semantic_frontdoor': semantic_frontdoor['trace'],
            'external_tools': external_trace,
            'archive_summaries': archive_trace,
            'frontdoor_context': {
                'source': frontdoor_context_source,
                'summary_turn_count': int(frontdoor_context.get('summary_turn_count', 0) or 0),
                'raw_tail_turns': int(frontdoor_context.get('raw_tail_turns', 0) or 0),
                'has_summary': bool(str(frontdoor_context.get('summary_text') or '').strip()),
            },
            'retrieval_scope': {
                'mode': str(retrieval_scope.get('mode') or ''),
                'search_context_types': list(retrieval_scope['search_context_types']),
                'allowed_context_types': list(retrieval_scope['allowed_context_types']),
                'allowed_resource_record_ids': list(retrieval_scope['allowed_resource_record_ids']),
                'allowed_skill_record_ids': list(retrieval_scope['allowed_skill_record_ids']),
            },
            'active_tasks': {
                'count': active_task_count,
                'included': bool(active_task_message),
            },
            'memory_write_hint': {
                'triggered': bool(memory_write_terms and memory_write_visible),
                'matched_terms': list(memory_write_terms),
                'visible': memory_write_visible,
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

    def _select_skills(
        self,
        *,
        visible_skills: list[Any],
        top_k: int,
        token_budget: int,
        ranked_skill_ids: list[str] | None = None,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        ordered_skills = list(visible_skills or [])
        if ranked_skill_ids is not None:
            skill_map = {
                str(getattr(item, 'skill_id', '') or '').strip(): item
                for item in list(visible_skills or [])
                if str(getattr(item, 'skill_id', '') or '').strip()
            }
            ranked: list[Any] = []
            seen: set[str] = set()
            for skill_id in list(ranked_skill_ids or []):
                item = skill_map.get(str(skill_id or '').strip())
                if item is None:
                    continue
                normalized = str(getattr(item, 'skill_id', '') or '').strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                ranked.append(item)
            ordered_skills = ranked + [
                item
                for item in list(visible_skills or [])
                if str(getattr(item, 'skill_id', '') or '').strip() not in seen
            ]

        selected: list[Any] = []
        trace: list[dict[str, Any]] = []
        used_tokens = 0
        semantic_rank_map = {
            str(skill_id or '').strip(): index + 1
            for index, skill_id in enumerate(list(ranked_skill_ids or []))
            if str(skill_id or '').strip()
        }
        for item in ordered_skills:
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
                    'tokens': line_tokens,
                    'semantic_rank': semantic_rank_map.get(str(getattr(item, 'skill_id', '') or '').strip()),
                }
            )
        if not selected:
            selected = ordered_skills[:top_k]
            trace = [
                {
                    'skill_id': getattr(item, 'skill_id', ''),
                    'tokens': 0,
                    'semantic_rank': semantic_rank_map.get(str(getattr(item, 'skill_id', '') or '').strip()),
                }
                for item in selected
            ]
        return selected, trace

    def _build_external_tool_block(
        self,
        *,
        visible_families: list[Any],
        visible_tool_names: list[str],
        top_k: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        visible_name_set = {
            str(name or '').strip()
            for name in list(visible_tool_names or [])
            if str(name or '').strip()
        }
        eligible: list[Any] = []
        for family in list(visible_families or []):
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            install_dir = str(getattr(family, 'install_dir', '') or '').strip()
            callable_flag = bool(getattr(family, 'callable', True))
            available_flag = bool(getattr(family, 'available', True))
            registered_callable = self._family_has_registered_callable_executor(family, visible_name_set)
            if not tool_id:
                continue
            if callable_flag and available_flag and registered_callable:
                continue
            if not install_dir and not callable_flag:
                continue
            eligible.append(family)
        if not eligible:
            return '', []

        lines = ['## Tool Resources That Require `load_tool_context`']
        trace: list[dict[str, Any]] = []
        for family in eligible[: max(1, top_k)]:
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            display_name = str(getattr(family, 'display_name', '') or tool_id).strip() or tool_id
            description = str(getattr(family, 'description', '') or '').strip()
            install_dir = str(getattr(family, 'install_dir', '') or '').strip()
            callable_flag = bool(getattr(family, 'callable', True))
            available_flag = bool(getattr(family, 'available', True))
            registered_callable = self._family_has_registered_callable_executor(family, visible_name_set)
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
                if available_flag and not registered_callable:
                    state_text = "Status: enabled but not currently registered in the callable function tool list for this turn"
                    next_step = f"For repair steps or usage guidance, call `load_tool_context(tool_id=\"{tool_id}\")`."
                elif not available_flag and registered_callable:
                    state_text = "Status: unavailable but exposed in the callable function tool list as `【待修复】`"
                    next_step = (
                        f"If you call `{tool_id}`, it will return structured repair guidance instead of executing the real capability. "
                        f"For repair steps or usage guidance, call `load_tool_context(tool_id=\"{tool_id}\")`."
                    )
                else:
                    state_text = "Status: unavailable"
                    next_step = f"For repair steps or usage guidance, call `load_tool_context(tool_id=\"{tool_id}\")`."
                if issue_summary:
                    state_text = f"{state_text} ({issue_summary})"
                line = (
                    f"- `{tool_id}` ({display_name}): {description or 'Tool guidance resource.'} {state_text}. "
                    f"{next_step}"
                )
            line_tokens = estimate_tokens(line)
            lines.append(line)
            trace.append(
                {
                    'tool_id': tool_id,
                    'install_dir': install_dir,
                    'callable': callable_flag,
                    'available': available_flag,
                    'registered_callable': registered_callable,
                    'tokens': line_tokens,
                }
            )
        return '\n'.join(lines), trace

    @staticmethod
    def _family_has_registered_callable_executor(family: Any, visible_name_set: set[str]) -> bool:
        executor_names: set[str] = set()
        for action in list(getattr(family, 'actions', []) or []):
            executor_names.update(
                str(name or '').strip()
                for name in list(getattr(action, 'executor_names', []) or [])
                if str(name or '').strip()
            )
        return bool(executor_names & visible_name_set)

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
        ranked_tool_ids: list[str] | None = None,
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

        if ranked_tool_ids is not None:
            family_map = {
                str(getattr(family, 'tool_id', '') or '').strip(): family
                for family in list(visible_families or [])
                if str(getattr(family, 'tool_id', '') or '').strip()
            }
            picked_extension: list[str] = []
            for tool_id in list(ranked_tool_ids or []):
                family = family_map.get(str(tool_id or '').strip())
                if family is None:
                    continue
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
                for name in sorted(set(family_names)):
                    if name in selected or name in picked_extension:
                        continue
                    picked_extension.append(name)
                    if len(picked_extension) >= extension_top_k:
                        break
                if len(picked_extension) >= extension_top_k:
                    break

            if len(picked_extension) < extension_top_k:
                for name in visible_names:
                    normalized = str(name or '').strip()
                    if (
                        normalized
                        and normalized in visible_set
                        and normalized not in selected
                        and normalized not in picked_extension
                        and normalized not in reserved_set
                    ):
                        picked_extension.append(normalized)
                        if len(picked_extension) >= extension_top_k:
                            break

            ordered = (
                reserved
                + [name for name in visible_names if name in selected]
                + [name for name in visible_names if name in picked_extension]
            )
            return ordered, {'reserved': reserved, 'core': sorted(selected), 'extension': picked_extension}

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
