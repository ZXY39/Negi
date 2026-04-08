from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace
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
    def _skill_id(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get('skill_id') or '').strip()
        return str(getattr(item, 'skill_id', '') or '').strip()

    @classmethod
    def _visible_skill_ids(cls, visible_skills: list[Any]) -> list[str]:
        ids: list[str] = []
        for item in list(visible_skills or []):
            skill_id = cls._skill_id(item)
            if not skill_id or skill_id in ids:
                continue
            ids.append(skill_id)
        return ids

    @staticmethod
    def _tool_id(item: Any) -> str:
        if isinstance(item, dict):
            return str(item.get('tool_id') or '').strip()
        return str(getattr(item, 'tool_id', '') or '').strip()

    @classmethod
    def _visible_tool_ids(cls, visible_families: list[Any]) -> list[str]:
        ids: list[str] = []
        for item in list(visible_families or []):
            tool_id = cls._tool_id(item)
            if not tool_id or tool_id in ids:
                continue
            ids.append(tool_id)
        return ids

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

    @staticmethod
    def _join_turn_overlay_sections(parts: list[str] | None) -> str:
        sections = [str(part or '').strip() for part in list(parts or []) if str(part or '').strip()]
        return '\n\n'.join(sections).strip()

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
                self._skill_id(item): item
                for item in list(visible_skills or [])
                if self._skill_id(item)
            }
            ranked: list[Any] = []
            seen: set[str] = set()
            for skill_id in list(ranked_skill_ids or []):
                item = skill_map.get(str(skill_id or '').strip())
                if item is None:
                    continue
                normalized = self._skill_id(item)
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                ranked.append(SimpleNamespace(**item) if isinstance(item, dict) else item)
            ordered_skills = ranked + [
                (SimpleNamespace(**item) if isinstance(item, dict) else item)
                for item in list(visible_skills or [])
                if self._skill_id(item) not in seen
            ]
        selected = list(ordered_skills[: max(1, int(top_k or 1))])
        semantic_rank_map = {
            str(skill_id or '').strip(): index + 1
            for index, skill_id in enumerate(list(ranked_skill_ids or []))
            if str(skill_id or '').strip()
        }
        trace = [
            {
                'skill_id': self._skill_id(item),
                'semantic_rank': semantic_rank_map.get(self._skill_id(item)),
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
        l0_only: bool = False,
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
            if not install_dir and not callable_flag and not l0_only:
                continue
            eligible.append(family)
        if not eligible:
            return '', []

        lines = ['## Tool Resources That Require `load_tool_context`']
        trace: list[dict[str, Any]] = []
        for family in eligible[: max(1, int(top_k or 1))]:
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            display_name = str(getattr(family, 'display_name', '') or tool_id).strip() or tool_id
            raw_description = str(getattr(family, 'description', '') or '').strip()
            description = self._tool_l0_summary(family) if l0_only else raw_description
            install_dir = str(getattr(family, 'install_dir', '') or '').strip()
            callable_flag = bool(getattr(family, 'callable', True))
            available_flag = bool(getattr(family, 'available', True))
            registered_callable = self._family_has_registered_callable_executor(family, visible_name_set)
            issue_summary = self._tool_family_issue_summary(family)
            if not callable_flag:
                state_text = f"Install dir: `{install_dir}`" if install_dir else "Install dir not configured"
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

    @staticmethod
    def _first_sentence(text: str) -> str:
        value = str(text or '').strip()
        if not value:
            return ''
        match = re.match(r'^(.*?[.!?。！？])(?:\s+|$)', value)
        if match:
            return str(match.group(1) or '').strip()
        return value

    @classmethod
    def _tool_l0_summary(cls, family: Any) -> str:
        metadata = getattr(family, 'metadata', {}) or {}
        l0 = str(metadata.get('l0') or getattr(family, 'l0', '') or '').strip()
        if l0:
            return cls._first_sentence(l0)
        description = str(getattr(family, 'description', '') or '').strip()
        return cls._first_sentence(description)

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

    @staticmethod
    def _visible_only_skill_trace(visible_skills: list[Any]) -> list[dict[str, Any]]:
        return [
            {
                'skill_id': CeoMessageBuilder._skill_id(item),
                'semantic_rank': None,
            }
            for item in list(visible_skills or [])
            if CeoMessageBuilder._skill_id(item)
        ]

    @classmethod
    def _visible_only_skill_items(cls, visible_skills: list[Any]) -> list[Any]:
        selected: list[Any] = []
        for item in list(visible_skills or []):
            if isinstance(item, dict):
                skill_id = str(item.get('skill_id') or '').strip()
                if not skill_id:
                    continue
                l0 = str(item.get('l0') or '').strip()
                description = str(item.get('description') or '').strip()
                summary = cls._first_sentence(l0 or description)
                selected.append({**item, 'description': summary, 'l0': summary or l0})
                continue
            skill_id = str(getattr(item, 'skill_id', '') or '').strip()
            if not skill_id:
                continue
            l0 = str(getattr(item, 'l0', '') or '').strip()
            description = str(getattr(item, 'description', '') or '').strip()
            summary = cls._first_sentence(l0 or description)
            selected.append(
                SimpleNamespace(
                    skill_id=skill_id,
                    display_name=str(getattr(item, 'display_name', '') or '').strip(),
                    description=summary,
                    l0=summary or l0,
                )
            )
        return selected

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
            loop=self._loop,
            memory_manager=memory_manager,
            query_text=query_text,
            visible_skills=visible_skills,
            visible_families=visible_families,
            skill_limit=max(inventory_top_k * 4, len(visible_skills), inventory_top_k, 8),
            tool_limit=max(extension_top_k * 4, len(visible_families), extension_top_k, 8),
        )
        semantic_trace = {
            'mode': str(semantic_frontdoor.get('mode') or '').strip(),
            'available': bool(semantic_frontdoor.get('available', False)),
            **dict(semantic_frontdoor.get('trace') or {}),
        }
        visible_only_mode = str(semantic_frontdoor.get('mode') or '').strip().lower() == 'unavailable'
        if visible_only_mode:
            selected_skills = self._visible_only_skill_items(visible_skills)
            skill_trace = self._visible_only_skill_trace(selected_skills)
            semantic_trace = {**semantic_trace, 'mode': 'visible_only', 'available': False}
        else:
            selected_skills, skill_trace = self._select_skills(
                visible_skills=visible_skills,
                top_k=inventory_top_k,
                ranked_skill_ids=list(semantic_frontdoor.get('skill_ids') or []),
            )
        split_prompt_builder = (
            callable(getattr(self._prompt_builder, 'build_base_prompt', None))
            and callable(getattr(self._prompt_builder, 'build_visible_skills_block', None))
        )
        if split_prompt_builder:
            system_prompt = self._prompt_builder.build_base_prompt()
            visible_skills_block = self._prompt_builder.build_visible_skills_block(skills=list(selected_skills))
        else:
            system_prompt = self._prompt_builder.build(skills=list(selected_skills))
            visible_skills_block = ''
        external_tools_block, external_trace = self._build_external_tool_block(
            visible_families=visible_families,
            visible_tool_names=list(exposure.get('tool_names') or []),
            top_k=(len(visible_families) if visible_only_mode else 8),
            l0_only=visible_only_mode,
        )
        if external_tools_block:
            system_prompt = f"{system_prompt}\n\n{external_tools_block}"

        memory_write_visible = 'memory_write' in {
            str(name or '').strip()
            for name in list(exposure.get('tool_names') or [])
            if str(name or '').strip()
        }
        turn_overlay_parts: list[str] = []
        if split_prompt_builder and visible_skills_block:
            turn_overlay_parts.append(visible_skills_block)
        if memory_write_terms and memory_write_visible:
            if split_prompt_builder:
                turn_overlay_parts.append(self._memory_write_hint_block(memory_write_terms))
            else:
                system_prompt = f"{system_prompt}\n\n{self._memory_write_hint_block(memory_write_terms)}"

        if visible_only_mode:
            visible_tool_names = list(exposure.get('tool_names') or [])
            selected_tool_names = []
            seen_tool_names: set[str] = set()
            for name in visible_tool_names:
                normalized = str(name or '').strip()
                if not normalized or normalized in seen_tool_names:
                    continue
                seen_tool_names.add(normalized)
                selected_tool_names.append(normalized)
            tool_trace = {
                'mode': 'visible_only',
                'reserved': [name for name in selected_tool_names if name in self.RESERVED_INTERNAL_TOOLS],
                'core': [],
                'extension': [name for name in selected_tool_names if name not in self.RESERVED_INTERNAL_TOOLS],
            }
        else:
            selected_tool_names, tool_trace = self._select_tools(
                query_text=query_text,
                visible_names=list(exposure.get('tool_names') or []),
                visible_families=visible_families,
                core_tools=core_tools,
                extension_top_k=extension_top_k,
                ranked_tool_ids=list(semantic_frontdoor.get('tool_ids') or []),
            )

        retrieval_scope = plan_retrieval_scope(
            visible_skills=visible_skills,
            visible_families=visible_families,
            semantic_frontdoor=semantic_frontdoor,
        )
        if visible_only_mode:
            targeted_skill_ids = self._visible_skill_ids(visible_skills)
            targeted_tool_ids = self._visible_tool_ids(visible_families)
            search_context_types = ['memory']
            if targeted_skill_ids:
                search_context_types.append('skill')
            if targeted_tool_ids:
                search_context_types.append('resource')
            retrieval_scope = {
                'mode': 'visible_only',
                'search_context_types': search_context_types,
                'allowed_context_types': list(search_context_types),
                'allowed_resource_record_ids': [f'tool:{item}' for item in targeted_tool_ids],
                'allowed_skill_record_ids': [f'skill:{item}' for item in targeted_skill_ids],
            }
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
            if split_prompt_builder:
                turn_overlay_parts.append(self._retrieved_memory_resolution_hint_block())
            else:
                retrieved_markdown = f"{self._retrieved_memory_resolution_hint_block()}\n\n{retrieved_markdown}"
        if split_prompt_builder and retrieved_markdown:
            turn_overlay_parts.append(retrieved_markdown)

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
        turn_overlay_text = self._join_turn_overlay_sections(turn_overlay_parts) if split_prompt_builder else ''
        model_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if retrieved_markdown and not split_prompt_builder:
            model_messages.append({"role": "assistant", "content": retrieved_markdown})
        model_messages.extend(history_messages)
        if not current_user_in_history:
            model_messages.append({"role": "user", "content": user_content if user_content is not None else query_text})

        trace = {
            'selected_skills': skill_trace,
            'selected_tools': tool_trace,
            'semantic_frontdoor': semantic_trace,
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
            'stable_prefix_message_count': len(model_messages),
            'turn_overlay_present': bool(turn_overlay_text),
            'turn_overlay_section_count': len(turn_overlay_parts),
            'turn_overlay_character_count': len(turn_overlay_text),
            'turn_overlay_text_hash': (
                hashlib.sha256(turn_overlay_text.encode('utf-8')).hexdigest()
                if turn_overlay_text
                else ''
            ),
            'stable_prompt_split': split_prompt_builder,
        }
        return ContextAssemblyResult(
            model_messages=model_messages,
            tool_names=selected_tool_names,
            trace=trace,
            turn_overlay_text=turn_overlay_text,
        )
