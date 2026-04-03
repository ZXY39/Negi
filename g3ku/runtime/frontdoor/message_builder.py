from __future__ import annotations

import re
from typing import Any

from g3ku.runtime.context.semantic_scope import plan_retrieval_scope, semantic_catalog_rankings
from g3ku.runtime.context.summarizer import score_query
from g3ku.runtime.context.types import ContextAssemblyResult, RetrievedContextBundle
from g3ku.runtime.core_tools import resolve_core_tool_targets
from g3ku.runtime.web_ceo_sessions import transcript_messages


class CeoMessageBuilder:
    MEMORY_WRITE_STRONG_TERMS: tuple[str, ...] = (
        '璁颁綇',
        '璇疯浣?',
        '涓嶈鍐?',
        '鍒啀',
        '榛樿',
        '闀挎湡鎸夎繖涓潵',
        'please remember',
        'from now on',
        'going forward',
        'default to',
        "don't use",
        'never use',
    )
    MEMORY_WRITE_FUTURE_RE = re.compile(r'(浠ュ悗|浠婂悗).{0,24}(閮絴榛樿|涓嶈|鍒啀|鎸夎繖涓獆鐢?)')
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
            if '浠ュ悗' in text:
                matched.append('浠ュ悗')
            if '浠婂悗' in text:
                matched.append('浠婂悗')
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
    def _content_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    text = item.strip()
                    if text:
                        parts.append(text)
                    continue
                if not isinstance(item, dict):
                    continue
                text = item.get("text", item.get("content", ""))
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
            return "\n".join(parts).strip()
        return str(value or "")

    @staticmethod
    def _history_message(message: dict[str, Any]) -> dict[str, Any]:
        entry = {
            'role': str(message.get('role') or '').strip().lower(),
            'content': message.get('content'),
        }
        for key in ('tool_calls', 'tool_call_id', 'name', 'metadata'):
            if key in message:
                entry[key] = message[key]
        return entry

    def _transcript_history(self, persisted_session: Any | None) -> list[dict[str, Any]]:
        if persisted_session is None:
            return []
        return [
            self._history_message(message)
            for message in transcript_messages(persisted_session)
        ]

    def _checkpoint_history(self, checkpoint_messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        history = [
            self._history_message(message)
            for message in list(checkpoint_messages or [])
            if isinstance(message, dict)
            and str(message.get('role') or '').strip().lower() in {'user', 'assistant', 'tool'}
        ]
        while history:
            first = history[0]
            if str(first.get('role') or '').strip().lower() != 'assistant':
                break
            if '## Retrieved Context' not in self._content_text(first.get('content')):
                break
            history.pop(0)
        return history

    @staticmethod
    def _history_is_semantically_complete(history_messages: list[dict[str, Any]] | None) -> bool:
        messages = [message for message in list(history_messages or []) if isinstance(message, dict)]
        if not messages:
            return False
        return str(messages[-1].get('role') or '').strip().lower() == 'assistant'

    def _history_has_current_user(
        self,
        *,
        history_messages: list[dict[str, Any]] | None,
        query_text: str,
        user_metadata: dict[str, Any] | None,
    ) -> bool:
        messages = [message for message in list(history_messages or []) if isinstance(message, dict)]
        if not messages:
            return False
        last = dict(messages[-1])
        if str(last.get('role') or '').strip().lower() != 'user':
            return False
        last_metadata = last.get('metadata') if isinstance(last.get('metadata'), dict) else {}
        turn_id = str((user_metadata or {}).get('_transcript_turn_id') or '').strip()
        last_turn_id = str(last_metadata.get('_transcript_turn_id') or '').strip()
        if turn_id and last_turn_id:
            return last_turn_id == turn_id
        return self._content_text(last.get('content')).strip() == str(query_text or '').strip()

    def _transcript_has_current_user(
        self,
        *,
        persisted_session: Any | None,
        query_text: str,
        user_metadata: dict[str, Any] | None,
    ) -> bool:
        if persisted_session is None:
            return False
        messages = [
            message
            for message in list(getattr(persisted_session, 'messages', []) or [])
            if isinstance(message, dict) and str(message.get('role') or '').strip().lower() in {'user', 'assistant'}
        ]
        if not messages:
            return False
        last = dict(messages[-1])
        if str(last.get('role') or '').strip().lower() != 'user':
            return False
        last_metadata = last.get('metadata') if isinstance(last.get('metadata'), dict) else {}
        turn_id = str((user_metadata or {}).get('_transcript_turn_id') or '').strip()
        if turn_id and str(last_metadata.get('_transcript_turn_id') or '').strip() == turn_id:
            return True
        return self._content_text(last.get('content')).strip() == str(query_text or '').strip()

    def _render_retrieved_context(self, bundle: RetrievedContextBundle) -> str:
        records = list(bundle.records or [])
        if not records:
            return ''
        lines = ['## Retrieved Context']
        for record in records:
            record_id = str(record.get('record_id') or '').strip() or 'unknown'
            context_type = str(record.get('context_type') or '').strip()
            l0 = str(record.get('l0') or '').strip()
            l1 = str(record.get('l1') or '').strip()
            l2 = str(record.get('l2_preview') or '').strip()
            header_text = l0 or l1 or ''
            prefix = f"{context_type}:" if context_type else ''
            lines.append(f"- [{prefix}{record_id}] {header_text}".rstrip())
            if l1 and l1 != header_text:
                lines.append(f"  L1: {l1}")
            if l2:
                lines.append(f"  L2: {l2}")
        return '\n'.join(lines).strip()

    def _select_skills(
        self,
        *,
        visible_skills: list[Any],
        top_k: int,
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
        selected = list(ordered_skills[: max(1, int(top_k or 1))])
        semantic_rank_map = {
            str(skill_id or '').strip(): index + 1
            for index, skill_id in enumerate(list(ranked_skill_ids or []))
            if str(skill_id or '').strip()
        }
        trace = [
            {
                'skill_id': getattr(item, 'skill_id', ''),
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
        for family in eligible[: max(1, int(top_k or 1))]:
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
            lines.append(line)
            trace.append(
                {
                    'tool_id': tool_id,
                    'install_dir': install_dir,
                    'callable': callable_flag,
                    'available': available_flag,
                    'registered_callable': registered_callable,
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
        return '; '.join((errors + warnings)[:2])

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
        for _score, names, _tool_id in ext_scored:
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

    async def build_for_ceo(
        self,
        *,
        session: Any,
        query_text: str,
        exposure: dict[str, Any],
        persisted_session: Any | None,
        checkpoint_messages: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
        user_metadata: dict[str, Any] | None = None,
    ) -> ContextAssemblyResult:
        main_service = getattr(self._loop, 'main_task_service', None)
        memory_manager = getattr(self._loop, 'memory_manager', None)
        assembly_cfg = getattr(getattr(self._loop, '_memory_runtime_settings', None), 'assembly', None)
        inventory_top_k = max(1, int(getattr(assembly_cfg, 'skill_inventory_top_k', 8) or 8))
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
            ranked_skill_ids=semantic_frontdoor['skill_ids'],
        )
        system_prompt = self._prompt_builder.build(skills=list(selected_skills))
        external_tools_block, external_trace = self._build_external_tool_block(
            visible_families=visible_families,
            visible_tool_names=list(exposure.get('tool_names') or []),
            top_k=8,
        )
        if external_tools_block:
            system_prompt = f"{system_prompt}\n\n{external_tools_block}"

        memory_write_visible = 'memory_write' in {
            str(name or '').strip()
            for name in list(exposure.get('tool_names') or [])
            if str(name or '').strip()
        }
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

        retrieved_bundle = RetrievedContextBundle(query=query_text)
        if memory_manager is not None and query_text:
            try:
                retrieved_bundle = await memory_manager.retrieve_context_bundle(
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
                retrieved_bundle = RetrievedContextBundle(query=query_text)

        retrieved_markdown = self._render_retrieved_context(retrieved_bundle)
        if memory_write_terms and retrieved_markdown:
            retrieved_markdown = f"{self._retrieved_memory_resolution_hint_block()}\n\n{retrieved_markdown}"

        checkpoint_history = self._checkpoint_history(checkpoint_messages)
        transcript_history = self._transcript_history(persisted_session)
        current_user_in_checkpoint = self._history_has_current_user(
            history_messages=checkpoint_history,
            query_text=query_text,
            user_metadata=user_metadata,
        )
        use_checkpoint_history = bool(checkpoint_history) and (
            self._history_is_semantically_complete(checkpoint_history) or current_user_in_checkpoint
        )
        history_messages = checkpoint_history if use_checkpoint_history else transcript_history
        current_user_in_history = self._history_has_current_user(
            history_messages=history_messages,
            query_text=query_text,
            user_metadata=user_metadata,
        )
        current_user_in_transcript = self._transcript_has_current_user(
            persisted_session=persisted_session,
            query_text=query_text,
            user_metadata=user_metadata,
        )
        model_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if retrieved_markdown:
            model_messages.append({"role": "assistant", "content": retrieved_markdown})
        model_messages.extend(history_messages)
        if not current_user_in_history:
            model_messages.append({"role": "user", "content": user_content if user_content is not None else query_text})

        trace = {
            'selected_skills': skill_trace,
            'selected_tools': tool_trace,
            'semantic_frontdoor': semantic_frontdoor['trace'],
            'external_tools': external_trace,
            'retrieval_scope': {
                'mode': str(retrieval_scope.get('mode') or ''),
                'search_context_types': list(retrieval_scope['search_context_types']),
                'allowed_context_types': list(retrieval_scope['allowed_context_types']),
                'allowed_resource_record_ids': list(retrieval_scope['allowed_resource_record_ids']),
                'allowed_skill_record_ids': list(retrieval_scope['allowed_skill_record_ids']),
            },
            'memory_write_hint': {
                'triggered': bool(memory_write_terms and memory_write_visible),
                'matched_terms': list(memory_write_terms),
                'visible': memory_write_visible,
            },
            'history_source': 'checkpoint' if use_checkpoint_history else 'transcript',
            'checkpoint_message_count': len(checkpoint_history),
            'transcript_message_count': len(transcript_history),
            'current_user_in_checkpoint': current_user_in_checkpoint,
            'current_user_in_history': current_user_in_history,
            'current_user_in_transcript': current_user_in_transcript,
            'retrieved_record_count': len(list(retrieved_bundle.records or [])),
            'model_messages_count': len(model_messages),
        }
        return ContextAssemblyResult(
            model_messages=model_messages,
            tool_names=selected_tool_names,
            trace=trace,
        )
