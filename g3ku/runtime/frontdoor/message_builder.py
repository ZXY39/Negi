from __future__ import annotations

import hashlib
import re
from types import SimpleNamespace
from typing import Any

from g3ku.runtime.context.semantic_scope import plan_retrieval_scope, semantic_catalog_rankings
from g3ku.runtime.context.summarizer import score_query
from g3ku.runtime.context.types import ContextAssemblyResult, RetrievedContextBundle
from g3ku.runtime.core_tools import resolve_core_tool_targets
from g3ku.runtime.frontdoor.capability_snapshot import CapabilitySnapshot, build_capability_snapshot
from g3ku.runtime.frontdoor.prompt_cache_contract import DEFAULT_CACHE_FAMILY_REVISION
from g3ku.runtime.frontdoor.task_ledger import build_task_ledger_summary
from g3ku.runtime.web_ceo_sessions import (
    is_history_visible_message,
    message_metadata,
    message_role,
    normalize_task_memory,
    transcript_messages,
)


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
    def _capability_snapshot(exposure: dict[str, Any]) -> CapabilitySnapshot:
        existing = exposure.get('capability_snapshot')
        if isinstance(existing, CapabilitySnapshot):
            return existing
        return build_capability_snapshot(
            visible_skills=list(exposure.get('skills') or []),
            visible_families=list(exposure.get('tool_families') or []),
            visible_tool_names=list(exposure.get('tool_names') or []),
        )

    @staticmethod
    def _skill_label(item: Any) -> str:
        skill_id = CeoMessageBuilder._skill_id(item)
        if isinstance(item, dict):
            display_name = str(item.get('display_name') or '').strip()
        else:
            display_name = str(getattr(item, 'display_name', '') or '').strip()
        return display_name if display_name and display_name != skill_id else skill_id

    @classmethod
    def _skill_summary(cls, item: Any) -> str:
        if isinstance(item, dict):
            l0 = str(item.get('l0') or '').strip()
            description = str(item.get('description') or '').strip()
        else:
            l0 = str(getattr(item, 'l0', '') or '').strip()
            description = str(getattr(item, 'description', '') or '').strip()
        return cls._first_sentence(l0 or description)

    @classmethod
    def _build_turn_skill_overlay(
        cls,
        *,
        selected_skills: list[Any],
        capability_snapshot: CapabilitySnapshot,
        visible_only_mode: bool,
    ) -> str:
        skill_ids = [cls._skill_id(item) for item in list(selected_skills or []) if cls._skill_id(item)]
        if not skill_ids:
            return ''
        all_visible_skill_ids = set(capability_snapshot.visible_skill_ids)
        selected_skill_ids = set(skill_ids)
        if visible_only_mode and selected_skill_ids == all_visible_skill_ids and len(skill_ids) == len(all_visible_skill_ids):
            lines = [
                '## Skill Summaries For Visible Exposure',
                '- Semantic ranking is unavailable, so the full visible skill set is summarized below.',
            ]
        else:
            lines = [
                '## Skills Most Relevant To This Turn',
                '- These summaries are selected from the stable visible capability exposure for the current turn.',
            ]
        for item in list(selected_skills or []):
            skill_id = cls._skill_id(item)
            if not skill_id:
                continue
            label = cls._skill_label(item)
            summary = cls._skill_summary(item)
            if summary:
                lines.append(f'- `{skill_id}` ({label}): {summary}')
            else:
                lines.append(f'- `{skill_id}` ({label})')
        return '\n'.join(lines)

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
    def _history_message(message: Any) -> dict[str, Any]:
        entry = {
            'role': message_role(message),
            'content': message.get('content') if isinstance(message, dict) else getattr(message, 'content', ''),
        }
        metadata = message_metadata(message)
        if metadata:
            entry['metadata'] = metadata
        for key in ('tool_calls', 'tool_call_id', 'name'):
            if isinstance(message, dict):
                if key in message:
                    entry[key] = message[key]
                continue
            value = getattr(message, key, None)
            if value not in (None, '', [], {}):
                entry[key] = value
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
            if message_role(message) in {'user', 'assistant', 'tool'}
            and is_history_visible_message(message)
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
        messages = transcript_messages(persisted_session)
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

    @staticmethod
    def _task_ledger_state(persisted_session: Any | None) -> dict[str, Any]:
        if persisted_session is None:
            return normalize_task_memory({})
        metadata = getattr(persisted_session, 'metadata', None)
        payload = dict(metadata or {}) if isinstance(metadata, dict) else {}
        return normalize_task_memory(payload.get('last_task_memory', payload.get('lastTaskMemory')))

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

    @staticmethod
    def _group_retrieved_records(records: list[dict[str, Any]] | None) -> dict[str, list[dict[str, Any]]]:
        grouped: dict[str, list[dict[str, Any]]] = {"memory": [], "resource": [], "skill": []}
        for record in list(records or []):
            if not isinstance(record, dict):
                continue
            context_type = str(record.get("context_type") or "").strip().lower()
            if context_type not in grouped:
                grouped[context_type] = []
            grouped[context_type].append(record)
        return grouped

    @staticmethod
    def _is_same_session_turn_memory_record(
        record: dict[str, Any],
        *,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> bool:
        _ = channel, chat_id
        if str(record.get("context_type") or "").strip().lower() != "memory":
            return False
        if str(record.get("source") or "").strip().lower() != "turn":
            return False
        record_session_key = str(record.get("session_key") or "").strip()
        return bool(session_key and record_session_key and record_session_key == session_key)

    @classmethod
    def _filter_same_session_turn_memory_records(
        cls,
        bundle: RetrievedContextBundle,
        *,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> tuple[RetrievedContextBundle, int]:
        filtered_records: list[dict[str, Any]] = []
        filtered_count = 0
        for record in list(bundle.records or []):
            if (
                isinstance(record, dict)
                and cls._is_same_session_turn_memory_record(
                    record,
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                )
            ):
                filtered_count += 1
                continue
            filtered_records.append(record)
        if filtered_count == 0:
            return bundle, 0
        meta = dict(bundle.meta or {})
        meta["total"] = len(filtered_records)
        return (
            RetrievedContextBundle(
                query=str(bundle.query or ""),
                records=filtered_records,
                grouped=cls._group_retrieved_records(filtered_records),
                plan=list(bundle.plan or []),
                meta=meta,
                trace=dict(bundle.trace or {}),
            ),
            filtered_count,
        )

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

    def _trace_external_tool_families(
        self,
        *,
        visible_families: list[Any],
        visible_tool_names: list[str],
    ) -> list[dict[str, Any]]:
        visible_name_set = {
            str(name or '').strip()
            for name in list(visible_tool_names or [])
            if str(name or '').strip()
        }
        trace: list[dict[str, Any]] = []
        for family in list(visible_families or []):
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            callable_flag = bool(getattr(family, 'callable', True))
            available_flag = bool(getattr(family, 'available', True))
            registered_callable = self._family_has_registered_callable_executor(family, visible_name_set)
            if not tool_id:
                continue
            if callable_flag and available_flag and registered_callable:
                continue
            trace.append(
                {
                    'tool_id': tool_id,
                    'callable': callable_flag,
                    'available': available_flag,
                    'registered_callable': registered_callable,
                }
            )
        return trace

    @staticmethod
    def _first_sentence(text: str) -> str:
        value = str(text or '').strip()
        if not value:
            return ''
        match = re.match(r'^(.*?[.!?。！？])(?:\s+|$)', value)
        if match:
            return str(match.group(1) or '').strip()
        return value

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
            picked_extension: list[str] = []
            for executor_name in list(ranked_tool_ids or []):
                normalized = str(executor_name or '').strip()
                if (
                    not normalized
                    or normalized not in visible_set
                    or normalized in selected
                    or normalized in picked_extension
                    or normalized in reserved_set
                ):
                    continue
                picked_extension.append(normalized)
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

    async def _collect_turn_context_sources(
        self,
        *,
        session: Any,
        query_text: str,
        exposure: dict[str, Any],
        user_content: Any | None,
    ) -> dict[str, Any]:
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
        capability_snapshot = self._capability_snapshot(exposure)
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
        split_prompt_builder = callable(getattr(self._prompt_builder, 'build_base_prompt', None))
        if split_prompt_builder:
            system_prompt = self._prompt_builder.build_base_prompt()
        else:
            system_prompt = self._prompt_builder.build(skills=[])
        visible_skills_block = self._build_turn_skill_overlay(
            selected_skills=list(selected_skills),
            capability_snapshot=capability_snapshot,
            visible_only_mode=visible_only_mode,
        )
        if capability_snapshot.stable_catalog_message:
            system_prompt = f"{system_prompt}\n\n{capability_snapshot.stable_catalog_message}".strip()
        external_trace = self._trace_external_tool_families(
            visible_families=visible_families,
            visible_tool_names=list(exposure.get('tool_names') or []),
        )

        memory_write_visible = 'memory_write' in {
            str(name or '').strip()
            for name in list(exposure.get('tool_names') or [])
            if str(name or '').strip()
        }
        turn_overlay_parts: list[str] = []
        if visible_skills_block:
            turn_overlay_parts.append(visible_skills_block)
        if memory_write_terms and memory_write_visible:
            turn_overlay_parts.append(self._memory_write_hint_block(memory_write_terms))

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

        session_key = str(session.state.session_key or '')
        channel = str(getattr(session, '_memory_channel', getattr(session, '_channel', 'cli')) or 'cli')
        chat_id = str(getattr(session, '_memory_chat_id', getattr(session, '_chat_id', session_key)) or session_key)
        retrieved_bundle = RetrievedContextBundle(query=query_text)
        if memory_manager is not None and query_text:
            try:
                retrieved_bundle = await memory_manager.retrieve_context_bundle(
                    query=query_text,
                    session_key=session_key,
                    channel=channel,
                    chat_id=chat_id,
                    search_context_types=retrieval_scope['search_context_types'],
                    allowed_context_types=retrieval_scope['allowed_context_types'],
                    allowed_resource_record_ids=retrieval_scope['allowed_resource_record_ids'],
                    allowed_skill_record_ids=retrieval_scope['allowed_skill_record_ids'],
                    exclude_same_session_turn_memory=True,
                )
            except Exception:
                retrieved_bundle = RetrievedContextBundle(query=query_text)

        retrieved_bundle, same_session_turn_memory_filtered_count = self._filter_same_session_turn_memory_records(
            retrieved_bundle,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
        )
        retrieved_markdown = self._render_retrieved_context(retrieved_bundle)
        if memory_write_terms and retrieved_markdown:
            if split_prompt_builder:
                turn_overlay_parts.append(self._retrieved_memory_resolution_hint_block())
            else:
                retrieved_markdown = f"{self._retrieved_memory_resolution_hint_block()}\n\n{retrieved_markdown}"
        if retrieved_markdown:
            turn_overlay_parts.append(retrieved_markdown)

        return {
            'capability_snapshot': capability_snapshot,
            'memory_write_terms': memory_write_terms,
            'memory_write_visible': memory_write_visible,
            'selected_skills': selected_skills,
            'skill_trace': skill_trace,
            'semantic_trace': semantic_trace,
            'external_trace': external_trace,
            'selected_tool_names': selected_tool_names,
            'tool_trace': tool_trace,
            'retrieval_scope': retrieval_scope,
            'retrieved_bundle': retrieved_bundle,
            'retrieved_markdown': retrieved_markdown,
            'turn_overlay_parts': turn_overlay_parts,
            'split_prompt_builder': split_prompt_builder,
            'system_prompt': system_prompt,
            'same_session_turn_memory_filtered_count': same_session_turn_memory_filtered_count,
            'user_content': user_content if user_content is not None else query_text,
        }

    def _resolve_history_injection(
        self,
        *,
        persisted_session: Any | None,
        checkpoint_messages: list[dict[str, Any]] | None,
        query_text: str,
        user_metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
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
        return {
            'history_messages': history_messages,
            'history_source': 'checkpoint' if use_checkpoint_history else 'transcript',
            'checkpoint_history': checkpoint_history,
            'transcript_history': transcript_history,
            'current_user_in_checkpoint': current_user_in_checkpoint,
            'current_user_in_history': current_user_in_history,
            'current_user_in_transcript': current_user_in_transcript,
        }

    def _inject_turn_context(
        self,
        *,
        system_prompt: str,
        history_messages: list[dict[str, Any]],
        user_content: Any,
        split_prompt_builder: bool,
        turn_overlay_parts: list[str],
        current_user_in_history: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
        turn_overlay_text = self._join_turn_overlay_sections(turn_overlay_parts)
        stable_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        stable_messages.extend(history_messages)
        if not current_user_in_history:
            stable_messages.append({"role": "user", "content": user_content})
        dynamic_appendix_messages = [
            {"role": "assistant", "content": part}
            for part in turn_overlay_parts
            if str(part or "").strip()
        ]
        if split_prompt_builder:
            model_messages = list(stable_messages)
        else:
            model_messages = list(stable_messages)
            if dynamic_appendix_messages:
                model_messages = [
                    stable_messages[0],
                    *dynamic_appendix_messages,
                    *stable_messages[1:],
                ]
        return model_messages, stable_messages, dynamic_appendix_messages, turn_overlay_text

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
        context_sources = await self._collect_turn_context_sources(
            session=session,
            query_text=query_text,
            exposure=exposure,
            user_content=user_content,
        )
        history_state = self._resolve_history_injection(
            persisted_session=persisted_session,
            checkpoint_messages=checkpoint_messages,
            query_text=query_text,
            user_metadata=user_metadata,
        )
        task_ledger_state = self._task_ledger_state(persisted_session)
        task_ledger_text = build_task_ledger_summary(task_ledger_state)
        turn_overlay_parts = list(context_sources['turn_overlay_parts'])
        if task_ledger_text:
            insert_index = len(turn_overlay_parts)
            if str(context_sources['retrieved_markdown'] or '').strip():
                insert_index = max(0, insert_index - 1)
            turn_overlay_parts.insert(insert_index, task_ledger_text)
        model_messages, stable_messages, dynamic_appendix_messages, turn_overlay_text = self._inject_turn_context(
            system_prompt=str(context_sources['system_prompt'] or ''),
            history_messages=list(history_state['history_messages']),
            user_content=context_sources['user_content'],
            split_prompt_builder=bool(context_sources['split_prompt_builder']),
            turn_overlay_parts=turn_overlay_parts,
            current_user_in_history=bool(history_state['current_user_in_history']),
        )

        trace = {
            'selected_skills': list(context_sources['skill_trace']),
            'selected_tools': dict(context_sources['tool_trace']),
            'semantic_frontdoor': dict(context_sources['semantic_trace']),
            'external_tools': list(context_sources['external_trace']),
            'capability_snapshot': {
                'exposure_revision': str(context_sources['capability_snapshot'].exposure_revision or ''),
                'visible_skill_ids': list(context_sources['capability_snapshot'].visible_skill_ids),
                'visible_tool_ids': list(context_sources['capability_snapshot'].visible_tool_ids),
            },
            'retrieval_scope': {
                'mode': str(context_sources['retrieval_scope'].get('mode') or ''),
                'search_context_types': list(context_sources['retrieval_scope']['search_context_types']),
                'allowed_context_types': list(context_sources['retrieval_scope']['allowed_context_types']),
                'allowed_resource_record_ids': list(context_sources['retrieval_scope']['allowed_resource_record_ids']),
                'allowed_skill_record_ids': list(context_sources['retrieval_scope']['allowed_skill_record_ids']),
            },
            'memory_write_hint': {
                'triggered': bool(context_sources['memory_write_terms'] and context_sources['memory_write_visible']),
                'matched_terms': list(context_sources['memory_write_terms']),
                'visible': bool(context_sources['memory_write_visible']),
            },
            'history_source': str(history_state['history_source']),
            'checkpoint_message_count': len(history_state['checkpoint_history']),
            'transcript_message_count': len(history_state['transcript_history']),
            'current_user_in_checkpoint': bool(history_state['current_user_in_checkpoint']),
            'current_user_in_history': bool(history_state['current_user_in_history']),
            'current_user_in_transcript': bool(history_state['current_user_in_transcript']),
            'task_ledger_present': bool(task_ledger_text),
            'task_ledger_task_count': len(list(task_ledger_state.get('task_ids') or [])),
            'task_ledger_result_count': len(list(task_ledger_state.get('task_results') or [])),
            'retrieved_record_count': len(list(context_sources['retrieved_bundle'].records or [])),
            'same_session_turn_memory_filtered_count': int(context_sources['same_session_turn_memory_filtered_count']),
            'model_messages_count': len(model_messages),
            'stable_prefix_message_count': len(stable_messages),
            'dynamic_appendix_message_count': len(dynamic_appendix_messages),
            'turn_overlay_present': bool(turn_overlay_text),
            'turn_overlay_section_count': len(turn_overlay_parts),
            'turn_overlay_character_count': len(turn_overlay_text),
            'turn_overlay_text_hash': (
                hashlib.sha256(turn_overlay_text.encode('utf-8')).hexdigest()
                if turn_overlay_text
                else ''
            ),
            'stable_prompt_split': bool(context_sources['split_prompt_builder']),
            'context_collection': {
                'retrieved_record_count': len(list(context_sources['retrieved_bundle'].records or [])),
                'retrieval_scope_mode': str(context_sources['retrieval_scope'].get('mode') or ''),
                'retrieved_context_present': bool(context_sources['retrieved_markdown']),
            },
            'message_injection': {
                'history_source': str(history_state['history_source']),
                'history_message_count': len(history_state['history_messages']),
                'current_user_appended': not bool(history_state['current_user_in_history']),
                'retrieved_context_in_model_messages': bool(
                    context_sources['retrieved_markdown'] and not context_sources['split_prompt_builder']
                ),
            },
        }
        return ContextAssemblyResult(
            model_messages=model_messages,
            stable_messages=stable_messages,
            dynamic_appendix_messages=dynamic_appendix_messages,
            tool_names=list(context_sources['selected_tool_names']),
            trace=trace,
            turn_overlay_text=turn_overlay_text,
            cache_family_revision=(
                str(context_sources['capability_snapshot'].exposure_revision or '').strip()
                or DEFAULT_CACHE_FAMILY_REVISION
            ),
        )
