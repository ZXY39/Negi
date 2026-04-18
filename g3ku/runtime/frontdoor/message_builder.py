from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from loguru import logger

from g3ku.config.live_runtime import get_runtime_config
from g3ku.llm_config.runtime_resolver import resolve_chat_target
from g3ku.runtime.context.semantic_scope import plan_retrieval_scope, semantic_catalog_rankings
from g3ku.runtime.context.summarizer import score_query
from g3ku.runtime.context.types import ContextAssemblyResult, RetrievedContextBundle
from g3ku.runtime.core_tools import resolve_core_tool_targets
from g3ku.runtime.semantic_context_summary import (
    HERMES_FAILURE_COOLDOWN_SECONDS,
    G3KU_MIN_CONTEXT_FLOOR,
    build_global_summary_thresholds,
    build_long_context_summary_message,
    default_semantic_context_state,
    estimate_message_tokens,
    future_cooldown_until,
    normalize_summary_result,
    semantic_summary_refresh_decision,
    summarize_global_context_model_first,
)
from g3ku.runtime.stage_prompt_compaction import (
    STAGE_EXTERNALIZED_PREFIX,
    completed_stage_blocks,
    stage_prompt_prefix,
)
from g3ku.runtime.tool_visibility import (
    CEO_FIXED_BUILTIN_TOOL_NAMES,
    filter_tool_names_for_semantic_top_k,
    filter_visible_tool_families_for_semantic_top_k,
)
from g3ku.runtime.frontdoor.capability_snapshot import CapabilitySnapshot, build_capability_snapshot
from g3ku.runtime.frontdoor.canonical_context import combine_canonical_context, normalize_frontdoor_canonical_context
from g3ku.runtime.frontdoor.prompt_cache_contract import DEFAULT_CACHE_FAMILY_REVISION
from g3ku.runtime.frontdoor.raw_stage_renderer import retained_raw_stage_messages
from g3ku.runtime.frontdoor.task_ledger import build_task_ledger_summary
from g3ku.runtime.frontdoor.tool_contract import build_frontdoor_tool_contract, upsert_frontdoor_tool_contract_message
from g3ku.runtime.web_ceo_sessions import (
    is_history_visible_message,
    message_metadata,
    message_role,
    normalize_task_memory,
    transcript_messages,
)

DEFAULT_FRONTDOOR_SKILL_INVENTORY_TOP_K = 16
DEFAULT_FRONTDOOR_EXTENSION_TOOL_TOP_K = 16


def _context_window_from_model_parameters(model_parameters: dict[str, Any] | None) -> int:
    payload = dict(model_parameters or {})
    for key in ("context_length", "context_window", "max_input_tokens", "max_context_tokens"):
        value = payload.get(key)
        try:
            resolved = int(value or 0)
        except (TypeError, ValueError):
            resolved = 0
        if resolved > 0:
            return resolved
    return 0


def _resolve_ceo_context_window_tokens(loop: Any) -> int:
    config = getattr(loop, "app_config", None)
    if config is None:
        try:
            config, _revision, _changed = get_runtime_config(force=False)
        except Exception:
            config = None
    if config is not None:
        try:
            model_key = config.resolve_scope_model_reference("ceo")
            target = resolve_chat_target(config, model_key)
            resolved = _context_window_from_model_parameters(getattr(target, "model_parameters", None))
            if resolved > 0:
                return resolved
        except Exception:
            pass
    try:
        resolved = int(getattr(loop, "context_length", 0) or 0)
    except (TypeError, ValueError):
        resolved = 0
    if resolved > 0:
        return resolved
    return G3KU_MIN_CONTEXT_FLOOR


def _externalized_completed_blocks_for_global_summary(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for item in list(messages or []):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if content.startswith(STAGE_EXTERNALIZED_PREFIX):
            selected.append(dict(item))
    return selected


class CeoMessageBuilder:
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
    MEMORY_WRITE_FUTURE_RE = re.compile(r'(以后|今后).{0,24}((?:都)?默认|不要|别再|按这个用?)')
    MEMORY_WRITE_REMEMBER_RE = re.compile(r'\bremember\b')
    MEMORY_WRITE_REMEMBER_EXCLUSION_RE = re.compile(r'\b(?:what|do)\s+you\s+remember\b')
    EXTENSION_TOOL_HINTS: dict[str, tuple[str, ...]] = {
        'cron': ('remind', 'schedule', 'cron', 'timer', 'recurring'),
        'load_skill_context': ('skill', 'workflow', 'procedure', 'steps', 'context', 'details'),
        'load_tool_context': ('tool', 'api', 'parameters', 'usage', 'context', 'details'),
        'filesystem': ('file', 'path', 'write', 'edit', 'delete', 'patch'),
        'filesystem_write': ('write', 'create', 'new file', 'save file', 'markdown file', '写入', '创建', '新建'),
        'filesystem_edit': ('edit', 'modify', 'update', 'append', 'prepend', 'insert', 'replace', 'line', '编辑', '修改', '更新', '追加', '插入', '替换'),
        'filesystem_copy': ('copy', 'duplicate', '复制', '拷贝'),
        'filesystem_move': ('move', 'rename', 'relocate', '移动', '重命名'),
        'filesystem_delete': ('delete', 'remove', 'cleanup', '删除', '移除', '清理'),
        'filesystem_propose_patch': ('patch', 'diff', 'propose patch', '补丁', '差异'),
        'exec': ('shell', 'command', 'bash', 'powershell', 'terminal', 'run'),
        'model_config': ('model', 'provider', 'config', 'token', 'temperature'),
    }
    RESERVED_INTERNAL_TOOLS: tuple[str, ...] = ("stop_tool_execution",)
    FIXED_BUILTIN_TOOL_NAMES: tuple[str, ...] = CEO_FIXED_BUILTIN_TOOL_NAMES

    def __init__(self, *, loop, prompt_builder) -> None:
        self._loop = loop
        self._prompt_builder = prompt_builder

    @staticmethod
    def _prompt_message_records(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for raw in list(messages or []):
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "").strip().lower()
            if not role:
                continue
            content = raw.get("content")
            if isinstance(content, str):
                content = content.strip()
                if not content:
                    continue
            elif content is None:
                continue
            records.append(
                {
                    "role": role,
                    "content": content,
                }
            )
        return records

    @classmethod
    def _request_body_seed_records(cls, messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for raw in list(messages or []):
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "").strip().lower()
            if not role:
                continue
            tool_calls = raw.get("tool_calls")
            structural_tool_call = isinstance(tool_calls, list) and bool(tool_calls)
            tool_call_id = str(raw.get("tool_call_id") or "").strip()
            tool_name = str(raw.get("name") or "").strip()
            preserve_structural_record = structural_tool_call or bool(tool_call_id) or bool(tool_name)
            content = raw.get("content")
            if isinstance(content, str):
                normalized_content = content.strip()
                if not normalized_content and not preserve_structural_record:
                    continue
                content = normalized_content if normalized_content else ""
            elif content is None:
                if not preserve_structural_record:
                    continue
                content = ""
            record: dict[str, Any] = {
                "role": role,
                "content": content,
            }
            if structural_tool_call:
                record["tool_calls"] = list(tool_calls)
            if tool_call_id:
                record["tool_call_id"] = tool_call_id
            if tool_name:
                record["name"] = tool_name
            records.append(record)
        return records

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
                '## 长期记忆写入提示',
                f'- 当前用户回合很可能在请求写入长期记忆（命中词：{terms}）。',
                '- 如果这是稳定的身份、偏好、约束、默认值、回避规则、工作流规则或项目长期事实，请在回复前调用 `memory_write`。',
                '- 不要把临时任务状态、猜测或未经确认的推断写入永久记忆。',
            ]
        )

    @staticmethod
    def _retrieved_memory_resolution_hint_block() -> str:
        return '\n'.join(
            [
                '## 已检索记忆使用提示',
                '- 下方已检索记忆中，已经包含与本轮相关、且此前确认过的用户默认值或偏好。',
                '- 如果用户正在询问默认值是什么，请直接复述已检索到的默认值。',
                '- 除非用户明确要求改规则，否则不要发明新的默认值、不要主动给出替代方案，也不要用通用最佳实践覆盖已检索到的默认值。',
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

    @classmethod
    def _semantic_tool_selection_inputs(
        cls,
        *,
        visible_families: list[Any],
        visible_tool_names: list[str],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        excluded_tool_names = set(cls.FIXED_BUILTIN_TOOL_NAMES)
        filtered_families = filter_visible_tool_families_for_semantic_top_k(
            visible_families,
            excluded_tool_names=excluded_tool_names,
        )
        filtered_tool_names = filter_tool_names_for_semantic_top_k(
            visible_tool_names,
            excluded_tool_names=excluded_tool_names,
        )
        return filtered_families, filtered_tool_names

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
                '## 本轮可见技能摘要',
                '- 当前语义排序不可用，因此下面按完整可见技能集给出摘要。',
                '- 如果当前还没有活动阶段且你需要使用工具，第一步必须先调用 `submit_next_stage`。',
                '- 如需读取某个 skill 的完整正文，仍然只能在当前已经存在活动阶段后调用 `load_skill_context`。',
            ]
        else:
            lines = [
                '## 本轮最相关的技能',
                '- 这些摘要来自当前轮稳定的可见能力曝光。',
                '- 如果当前还没有活动阶段且你需要使用工具，第一步必须先调用 `submit_next_stage`。',
                '- 如需读取某个 skill 的完整正文，仍然只能在当前已经存在活动阶段后调用 `load_skill_context`。',
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

    @classmethod
    def _build_turn_tool_overlay(
        cls,
        *,
        selected_tool_items: list[dict[str, str]],
        capability_snapshot: CapabilitySnapshot,
        visible_only_mode: bool,
    ) -> str:
        tool_items = [
            {
                'tool_id': str((item or {}).get('tool_id') or '').strip(),
                'description': str((item or {}).get('description') or '').strip(),
            }
            for item in list(selected_tool_items or [])
            if str((item or {}).get('tool_id') or '').strip()
        ]
        tool_names = [str(item.get('tool_id') or '').strip() for item in tool_items if str(item.get('tool_id') or '').strip()]
        if not tool_names:
            return ''
        all_visible_tool_names = set(capability_snapshot.visible_tool_ids)
        selected_tool_name_set = set(tool_names)
        if visible_only_mode and selected_tool_name_set == all_visible_tool_names and len(tool_names) == len(all_visible_tool_names):
            lines = [
                '## 本轮候选工具',
                '- 当前语义排序不可用，因此下面列出完整的可见具体工具集合。',
                '- 如果当前还没有活动阶段且你需要使用工具，第一步必须先调用 `submit_next_stage`。',
                '- 对非内置候选工具，如需读取完整工具契约，仅在当前已经存在活动阶段后调用 `load_tool_context(tool_id="...")`。',
            ]
        else:
            lines = [
                '## 本轮候选工具',
                '- 下面是本轮候选具体工具。',
                '- 如果当前还没有活动阶段且你需要使用工具，第一步必须先调用 `submit_next_stage`。',
                '- 对非内置候选工具，如需读取完整工具契约，仅在当前已经存在活动阶段后调用 `load_tool_context(tool_id="...")`。',
            ]
        lines.append(
            'candidate_tools = '
            + json.dumps(
                [
                    {
                        'tool_id': str(item.get('tool_id') or '').strip(),
                        'description': str(item.get('description') or '').strip(),
                    }
                    for item in tool_items
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return '\n'.join(lines)

    @classmethod
    def _callable_tool_names(
        cls,
        *,
        visible_tool_names: list[str],
        hydrated_tool_names: list[str] | None = None,
    ) -> list[str]:
        visible = [
            str(item or '').strip()
            for item in list(visible_tool_names or [])
            if str(item or '').strip()
        ]
        visible_set = set(visible)
        ordered: list[str] = []
        seen: set[str] = set()
        for name in [*cls.RESERVED_INTERNAL_TOOLS, *cls.FIXED_BUILTIN_TOOL_NAMES]:
            normalized = str(name or '').strip()
            if not normalized or normalized not in visible_set or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        for name in list(hydrated_tool_names or []):
            normalized = str(name or '').strip()
            if not normalized or normalized not in visible_set or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    @staticmethod
    def _elapsed_ms(started_at: float) -> float:
        return round(max(0.0, (time.perf_counter() - float(started_at))) * 1000.0, 3)

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
            if '## 已检索上下文' not in self._content_text(first.get('content')):
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
        lines = ['## 已检索上下文']
        for record in records:
            record_id = str(record.get('record_id') or '').strip() or '未知'
            context_type = str(record.get('context_type') or '').strip()
            l0 = str(record.get('l0') or '').strip()
            l1 = str(record.get('l1') or '').strip()
            l2 = str(record.get('l2_preview') or '').strip()
            header_text = l0 or l1 or ''
            prefix = f"{context_type}:" if context_type else ''
            lines.append(f"- [{prefix}{record_id}] {header_text}".rstrip())
            if l1 and l1 != header_text:
                lines.append(f"  一级摘要：{l1}")
            if l2:
                lines.append(f"  二级摘要：{l2}")
        return '\n'.join(lines).strip()

    @staticmethod
    def _assembly_cfg(loop: Any) -> Any:
        return getattr(getattr(loop, "_memory_runtime_settings", None), "assembly", None)

    @staticmethod
    def _tool_family_by_executor_name(visible_families: list[Any]) -> dict[str, Any]:
        family_by_executor: dict[str, Any] = {}
        for family in list(visible_families or []):
            tool_id = str(getattr(family, "tool_id", "") or "").strip()
            executor_names: list[str] = []
            for action in list(getattr(family, "actions", []) or []):
                for raw_name in list(getattr(action, "executor_names", []) or []):
                    name = str(raw_name or "").strip()
                    if name and name not in executor_names:
                        executor_names.append(name)
            if not executor_names and tool_id:
                executor_names.append(tool_id)
            for executor_name in executor_names:
                family_by_executor.setdefault(executor_name, family)
        return family_by_executor

    @staticmethod
    def _query_contains_any(query_text: str, patterns: tuple[str, ...]) -> bool:
        raw = str(query_text or '').strip()
        lower = raw.lower()
        for pattern in patterns:
            token = str(pattern or '').strip()
            if not token:
                continue
            if token.isascii():
                if token.lower() in lower:
                    return True
                continue
            if token in raw:
                return True
        return False

    @classmethod
    def _filesystem_intent_targets(cls, query_text: str) -> list[str]:
        targets: list[str] = []

        def _append(tool_id: str) -> None:
            normalized = str(tool_id or '').strip()
            if normalized and normalized not in targets:
                targets.append(normalized)

        if cls._query_contains_any(query_text, ('patch', 'diff', 'propose patch', '补丁', '差异')):
            _append('filesystem_propose_patch')
        if cls._query_contains_any(query_text, ('delete', 'remove', 'cleanup', '删除', '移除', '清理')):
            _append('filesystem_delete')
        if cls._query_contains_any(query_text, ('move', 'rename', 'relocate', '移动', '重命名')):
            _append('filesystem_move')
        if cls._query_contains_any(query_text, ('copy', 'duplicate', '复制', '拷贝')):
            _append('filesystem_copy')
        if cls._query_contains_any(
            query_text,
            (
                'append',
                'prepend',
                'insert',
                'replace',
                'modify',
                'update',
                'edit',
                'change',
                'line',
                '编辑',
                '修改',
                '更新',
                '追加',
                '插入',
                '替换',
                '改写',
                '行',
            ),
        ):
            _append('filesystem_edit')
        if cls._query_contains_any(
            query_text,
            (
                'write',
                'create',
                'new file',
                'generate file',
                'save file',
                'markdown file',
                '写入',
                '创建',
                '新建',
                '生成文件',
                '保存文件',
            ),
        ):
            _append('filesystem_write')
        return targets

    @classmethod
    def _tool_intent_bonus(cls, *, tool_name: str, family_tool_id: str, query_text: str) -> float:
        mutating_targets = cls._filesystem_intent_targets(query_text)
        if not mutating_targets:
            return 0.0
        normalized_tool_name = str(tool_name or '').strip()
        normalized_family_tool_id = str(family_tool_id or '').strip()
        if normalized_tool_name == 'exec':
            return -8.0
        if normalized_tool_name in mutating_targets:
            return 12.0 - float(mutating_targets.index(normalized_tool_name))
        if normalized_family_tool_id == 'filesystem':
            return 3.0
        return 0.0

    def _candidate_tool_prompt_items(
        self,
        *,
        selected_tool_names: list[str],
        visible_families: list[Any],
    ) -> list[dict[str, str]]:
        family_by_executor = self._tool_family_by_executor_name(visible_families)
        main_service = getattr(self._loop, 'main_task_service', None)
        get_tool_toolskill = getattr(main_service, 'get_tool_toolskill', None) if main_service is not None else None
        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_name in list(selected_tool_names or []):
            tool_id = str(raw_name or '').strip()
            if not tool_id or tool_id in seen:
                continue
            seen.add(tool_id)
            description = ''
            if callable(get_tool_toolskill):
                try:
                    toolskill_payload = dict(get_tool_toolskill(tool_id) or {})
                except Exception:
                    toolskill_payload = {}
                description = self._first_sentence(
                    str(toolskill_payload.get('description') or toolskill_payload.get('l0') or '').strip()
                )
            if not description:
                family = family_by_executor.get(tool_id)
                if family is None:
                    family = next(
                        (
                            item
                            for item in list(visible_families or [])
                            if str(getattr(item, 'tool_id', '') or '').strip() == tool_id
                        ),
                        None,
                    )
                description = self._first_sentence(str(getattr(family, 'description', '') or '').strip()) if family is not None else ''
            items.append(
                {
                    'tool_id': tool_id,
                    'description': description,
                }
            )
        return items

    @classmethod
    def _global_summary_settings(cls, loop: Any) -> dict[str, Any]:
        assembly_cfg = cls._assembly_cfg(loop)
        return {
            "trigger_ratio": float(getattr(assembly_cfg, "frontdoor_global_summary_trigger_ratio", 0.10) or 0.10),
            "target_ratio": float(getattr(assembly_cfg, "frontdoor_global_summary_target_ratio", 0.20) or 0.20),
            "min_output_tokens": int(getattr(assembly_cfg, "frontdoor_global_summary_min_output_tokens", 2000) or 2000),
            "max_output_ratio": float(getattr(assembly_cfg, "frontdoor_global_summary_max_output_ratio", 0.05) or 0.05),
            "max_output_tokens_ceiling": int(
                getattr(assembly_cfg, "frontdoor_global_summary_max_output_tokens_ceiling", 12000) or 12000
            ),
            "pressure_warn_ratio": float(getattr(assembly_cfg, "frontdoor_global_summary_pressure_warn_ratio", 0.85) or 0.85),
            "force_refresh_ratio": float(getattr(assembly_cfg, "frontdoor_global_summary_force_refresh_ratio", 0.95) or 0.95),
            "min_delta_tokens": int(getattr(assembly_cfg, "frontdoor_global_summary_min_delta_tokens", 2000) or 2000),
            "failure_cooldown_seconds": int(
                getattr(assembly_cfg, "frontdoor_global_summary_failure_cooldown_seconds", HERMES_FAILURE_COOLDOWN_SECONDS)
                or HERMES_FAILURE_COOLDOWN_SECONDS
            ),
            "model_key": str(getattr(assembly_cfg, "frontdoor_global_summary_model", "") or "").strip(),
        }

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _normalize_hidden_internal_summary_message(message: dict[str, Any]) -> dict[str, Any] | None:
        if message_role(message) != "assistant":
            return None
        metadata = message_metadata(message)
        source = str(metadata.get("source") or "").strip().lower()
        if source not in {"heartbeat", "cron"}:
            return None
        text_parts: list[str] = []
        content = str(message.get("content") or "").strip()
        if content:
            text_parts.append(content)
        canonical_context = (
            dict(message.get("canonical_context"))
            if isinstance(message.get("canonical_context"), dict)
            else {}
        )
        if canonical_context:
            text_parts.append(
                "Canonical context:\n"
                + json.dumps(canonical_context, ensure_ascii=False, sort_keys=True)
            )
        if not text_parts:
            return None
        return {"role": "assistant", "content": "\n\n".join(text_parts), "metadata": {"source": source}}

    @classmethod
    def _hidden_internal_summary_messages(
        cls,
        *,
        persisted_session: Any | None,
        checkpoint_messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for raw in list(getattr(persisted_session, "messages", []) or []) if persisted_session is not None else []:
            if isinstance(raw, dict):
                normalized = cls._normalize_hidden_internal_summary_message(raw)
                if normalized is not None:
                    items.append(normalized)
        for raw in list(checkpoint_messages or []):
            if isinstance(raw, dict):
                normalized = cls._normalize_hidden_internal_summary_message(raw)
                if normalized is not None and normalized not in items:
                    items.append(normalized)
        return items

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
            return [], {
                'mode': 'disabled',
                'reserved_internal_tool_names': [],
                'fixed_builtin_tool_names': [],
                'candidate_tool_names': [],
            }
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
            return ordered, {
                'mode': 'core_only',
                'reserved_internal_tool_names': reserved,
                'fixed_builtin_tool_names': sorted(selected),
                'candidate_tool_names': [],
            }

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
            return ordered, {
                'mode': 'dense_only',
                'reserved_internal_tool_names': reserved,
                'fixed_builtin_tool_names': sorted(selected),
                'candidate_tool_names': picked_extension,
            }

        visible_index = {
            str(name or '').strip(): index
            for index, name in enumerate(list(visible_names or []))
            if str(name or '').strip()
        }
        executor_family_map = self._tool_family_by_executor_name(visible_families)
        ext_scored: list[tuple[float, int, str]] = []
        for name in visible_names:
            normalized = str(name or '').strip()
            if (
                not normalized
                or normalized not in visible_set
                or normalized in core_resolution.executor_names
                or normalized in raw_core_entries
                or normalized in reserved_set
            ):
                continue
            family = executor_family_map.get(normalized)
            family_tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            family_display_name = str(getattr(family, 'display_name', '') or '').strip()
            family_description = str(getattr(family, 'description', '') or '').strip()
            score = score_query(
                query_text,
                normalized,
                family_display_name or normalized,
                family_description,
                family_tool_id,
            )
            for hint_owner in (normalized, family_tool_id):
                for hint in self.EXTENSION_TOOL_HINTS.get(hint_owner, ()):
                    if hint and hint.lower() in str(query_text or '').lower():
                        score += 5.0
            score += self._tool_intent_bonus(
                tool_name=normalized,
                family_tool_id=family_tool_id,
                query_text=query_text,
            )
            ext_scored.append((score, visible_index.get(normalized, len(visible_index) + 10_000), normalized))
        ext_scored.sort(key=lambda item: (-item[0], item[1], item[2]))

        picked_extension: list[str] = []
        for _score, _visible_rank, name in ext_scored:
            if len(picked_extension) >= extension_top_k:
                break
            if name in selected or name in picked_extension:
                continue
            picked_extension.append(name)
        if not picked_extension:
            for name in visible_names:
                normalized = str(name or '').strip()
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

        ordered = (
            reserved
            + [name for name in visible_names if name in selected]
            + [name for name in visible_names if name in picked_extension]
        )
        return ordered, {
            'mode': 'rbac_fallback',
            'reserved_internal_tool_names': reserved,
            'fixed_builtin_tool_names': sorted(selected),
            'candidate_tool_names': picked_extension,
        }

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
        hydrated_tool_names: list[str] | None = None,
    ) -> dict[str, Any]:
        semantic_started_at = 0.0
        semantic_elapsed_ms = 0.0
        retrieval_started_at = 0.0
        retrieval_elapsed_ms = 0.0
        main_service = getattr(self._loop, 'main_task_service', None)
        memory_manager = getattr(self._loop, 'memory_manager', None)
        assembly_cfg = getattr(getattr(self._loop, '_memory_runtime_settings', None), 'assembly', None)
        inventory_top_k = max(
            1,
            int(getattr(assembly_cfg, 'skill_inventory_top_k', DEFAULT_FRONTDOOR_SKILL_INVENTORY_TOP_K) or DEFAULT_FRONTDOOR_SKILL_INVENTORY_TOP_K),
        )
        extension_top_k = max(
            0,
            int(getattr(assembly_cfg, 'extension_tool_top_k', DEFAULT_FRONTDOOR_EXTENSION_TOOL_TOP_K) or DEFAULT_FRONTDOOR_EXTENSION_TOOL_TOP_K),
        )
        visible_tool_names = [
            str(name or '').strip()
            for name in list(exposure.get('tool_names') or [])
            if str(name or '').strip()
        ]
        callable_tool_names = self._callable_tool_names(
            visible_tool_names=visible_tool_names,
            hydrated_tool_names=list(hydrated_tool_names or []),
        )
        callable_tool_name_set = set(callable_tool_names)

        if main_service is not None and memory_manager is not None:
            try:
                maybe_refresh = getattr(main_service, 'maybe_refresh_external_resource_changes', None)
                if callable(maybe_refresh):
                    await maybe_refresh(
                        session_id=str(getattr(getattr(session, 'state', None), 'session_key', '') or 'web:shared').strip() or 'web:shared',
                    )
                await memory_manager.sync_catalog(main_service)
            except Exception:
                pass

        memory_write_terms = self._detect_memory_write_intent(query_text)
        visible_skills = list(exposure.get('skills') or [])
        visible_families = list(exposure.get('tool_families') or [])
        semantic_visible_families, semantic_visible_tool_names = self._semantic_tool_selection_inputs(
            visible_families=visible_families,
            visible_tool_names=visible_tool_names,
        )
        capability_snapshot = self._capability_snapshot(exposure)
        semantic_started_at = time.perf_counter()
        semantic_frontdoor = await semantic_catalog_rankings(
            loop=self._loop,
            memory_manager=memory_manager,
            query_text=query_text,
            visible_skills=visible_skills,
            visible_families=semantic_visible_families,
            skill_limit=max(inventory_top_k * 4, len(visible_skills), inventory_top_k, 8),
            tool_limit=max(extension_top_k * 4, len(semantic_visible_tool_names), extension_top_k, 8),
        )
        semantic_elapsed_ms = self._elapsed_ms(semantic_started_at)
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
        if capability_snapshot.stable_catalog_message:
            system_prompt = f"{system_prompt}\n\n{capability_snapshot.stable_catalog_message}".strip()
        external_trace = self._trace_external_tool_families(
            visible_families=visible_families,
            visible_tool_names=visible_tool_names,
        )

        memory_write_visible = 'memory_write' in {
            str(name or '').strip()
            for name in visible_tool_names
            if str(name or '').strip()
        }
        turn_overlay_parts: list[str] = []
        if memory_write_terms and memory_write_visible:
            turn_overlay_parts.append(self._memory_write_hint_block(memory_write_terms))

        if visible_only_mode:
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
                'reserved_internal_tool_names': [
                    name for name in selected_tool_names if name in self.RESERVED_INTERNAL_TOOLS
                ],
                'fixed_builtin_tool_names': [
                    name
                    for name in selected_tool_names
                    if name in self.FIXED_BUILTIN_TOOL_NAMES
                ],
                'hydrated_tool_names': [
                    name
                    for name in selected_tool_names
                    if name in callable_tool_name_set
                    and name not in self.RESERVED_INTERNAL_TOOLS
                    and name not in self.FIXED_BUILTIN_TOOL_NAMES
                ],
                'candidate_tool_names': [
                    name
                    for name in selected_tool_names
                    if name not in callable_tool_name_set
                    and name not in self.RESERVED_INTERNAL_TOOLS
                ],
            }
        else:
            ranked_tool_ids = [
                str(tool_id or '').strip()
                for tool_id in list(semantic_frontdoor.get('tool_ids') or [])
                if str(tool_id or '').strip()
            ]
            selected_tool_names, tool_trace = self._select_tools(
                query_text=query_text,
                visible_names=visible_tool_names,
                visible_families=visible_families,
                core_tools=set(callable_tool_names),
                extension_top_k=extension_top_k,
                ranked_tool_ids=(ranked_tool_ids if ranked_tool_ids else None),
            )
        candidate_tool_names = [
            str(name or '').strip()
            for name in list(tool_trace.get('candidate_tool_names') or [])
            if str(name or '').strip()
        ]
        candidate_tool_items = self._candidate_tool_prompt_items(
            selected_tool_names=list(candidate_tool_names),
            visible_families=visible_families,
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
            retrieval_started_at = time.perf_counter()
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
            retrieval_elapsed_ms = self._elapsed_ms(retrieval_started_at)

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
            'selected_tool_names': candidate_tool_names,
            'selected_tool_items': candidate_tool_items,
            'callable_tool_names': callable_tool_names,
            'hydrated_tool_names': [
                name
                for name in callable_tool_names
                if name not in self.RESERVED_INTERNAL_TOOLS
                and name not in self.FIXED_BUILTIN_TOOL_NAMES
            ],
            'tool_trace': tool_trace,
            'retrieval_scope': retrieval_scope,
            'retrieved_bundle': retrieved_bundle,
            'retrieved_markdown': retrieved_markdown,
            'turn_overlay_parts': turn_overlay_parts,
            'split_prompt_builder': split_prompt_builder,
            'system_prompt': system_prompt,
            'same_session_turn_memory_filtered_count': same_session_turn_memory_filtered_count,
            'user_content': user_content if user_content is not None else query_text,
            'span_timings_ms': {
                'semantic_catalog_rankings': semantic_elapsed_ms,
                'retrieve_context_bundle': retrieval_elapsed_ms,
            },
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
        history_seed = str((user_metadata or {}).get("_frontdoor_history_seed") or "").strip()
        if use_checkpoint_history and history_seed == "session_window":
            history_source = "session_window"
        else:
            history_source = "checkpoint" if use_checkpoint_history else "transcript"
        current_user_in_transcript = self._transcript_has_current_user(
            persisted_session=persisted_session,
            query_text=query_text,
            user_metadata=user_metadata,
        )
        return {
            'history_messages': history_messages,
            'history_source': history_source,
            'checkpoint_history': checkpoint_history,
            'transcript_history': transcript_history,
            'current_user_in_checkpoint': current_user_in_checkpoint,
            'current_user_in_history': current_user_in_history,
            'current_user_in_transcript': current_user_in_transcript,
        }

    def _frontdoor_prompt_history(
        self,
        *,
        history_state: dict[str, Any],
        frontdoor_stage_state: dict[str, Any] | None,
        query_text: str,
        user_metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        has_stage_state = bool(list(dict(frontdoor_stage_state or {}).get("stages") or []))
        history_source = str(history_state.get("history_source") or "").strip() or "transcript"
        history_messages = [
            dict(item)
            for item in list(history_state.get("history_messages") or [])
            if isinstance(item, dict)
        ]
        if has_stage_state and history_source == "checkpoint":
            transcript_history = [
                dict(item)
                for item in list(history_state.get("transcript_history") or [])
                if isinstance(item, dict)
            ]
            if transcript_history:
                history_messages = transcript_history
                history_source = "transcript"
            else:
                history_messages = [
                    dict(item)
                    for item in history_messages
                    if message_role(item) == "user"
                ]
        _prefix, cleaned_messages = stage_prompt_prefix(
            history_messages,
            preserve_leading_system=False,
            preserve_leading_user=False,
        )
        current_user_in_history = self._history_has_current_user(
            history_messages=cleaned_messages,
            query_text=query_text,
            user_metadata=user_metadata,
        )
        return {
            "history_messages": cleaned_messages,
            "history_source": history_source,
            "current_user_in_history": current_user_in_history,
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

    def _inject_direct_request_body_continuation(
        self,
        *,
        request_body_seed_messages: list[dict[str, Any]],
        user_content: Any,
        turn_overlay_parts: list[str],
        query_text: str,
        user_metadata: dict[str, Any] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str, bool]:
        turn_overlay_text = self._join_turn_overlay_sections(turn_overlay_parts)
        stable_messages = self._request_body_seed_records(request_body_seed_messages)
        current_user_in_history = self._history_has_current_user(
            history_messages=stable_messages,
            query_text=query_text,
            user_metadata=user_metadata,
        )
        live_messages = list(stable_messages)
        if not current_user_in_history:
            live_messages.append({"role": "user", "content": user_content})
        dynamic_appendix_messages = [
            {"role": "assistant", "content": part}
            for part in turn_overlay_parts
            if str(part or "").strip()
        ]
        model_messages = [*live_messages, *dynamic_appendix_messages]
        return model_messages, stable_messages, dynamic_appendix_messages, turn_overlay_text, current_user_in_history

    def _build_direct_request_body_continuation_result(
        self,
        *,
        context_sources: dict[str, Any],
        persisted_session: Any | None,
        request_body_seed_messages: list[dict[str, Any]],
        query_text: str,
        user_metadata: dict[str, Any] | None,
        semantic_context_state: dict[str, Any] | None,
        ephemeral_tail_messages: list[dict[str, Any]] | None,
        collect_elapsed_ms: float,
        history_elapsed_ms: float,
    ) -> ContextAssemblyResult:
        task_ledger_state = self._task_ledger_state(persisted_session)
        task_ledger_text = build_task_ledger_summary(task_ledger_state)
        turn_overlay_parts = list(context_sources['turn_overlay_parts'])
        if task_ledger_text:
            insert_index = len(turn_overlay_parts)
            if str(context_sources['retrieved_markdown'] or '').strip():
                insert_index = max(0, insert_index - 1)
            turn_overlay_parts.insert(insert_index, task_ledger_text)

        inject_started_at = time.perf_counter()
        (
            model_messages,
            stable_messages,
            dynamic_appendix_messages,
            turn_overlay_text,
            current_user_in_history,
        ) = self._inject_direct_request_body_continuation(
            request_body_seed_messages=request_body_seed_messages,
            user_content=context_sources['user_content'],
            turn_overlay_parts=turn_overlay_parts,
            query_text=query_text,
            user_metadata=user_metadata,
        )

        frontdoor_tool_contract = build_frontdoor_tool_contract(
            callable_tool_names=list(context_sources['callable_tool_names']),
            candidate_tool_names=list(context_sources['selected_tool_names']),
            candidate_tool_items=list(context_sources.get('selected_tool_items') or []),
            hydrated_tool_names=list(context_sources['hydrated_tool_names']),
            frontdoor_stage_state=dict(context_sources.get('frontdoor_stage_state') or {}),
            visible_skill_ids=[
                self._skill_id(item)
                for item in list(context_sources['selected_skills'] or [])
                if self._skill_id(item)
            ],
            candidate_skill_ids=[
                self._skill_id(item)
                for item in list(context_sources['selected_skills'] or [])
                if self._skill_id(item)
            ],
            rbac_visible_tool_names=list(context_sources['capability_snapshot'].visible_tool_ids),
            rbac_visible_skill_ids=list(context_sources['capability_snapshot'].visible_skill_ids),
            contract_revision=(
                str(context_sources['capability_snapshot'].exposure_revision or '').strip()
                or DEFAULT_CACHE_FAMILY_REVISION
            ),
            exec_runtime_policy=(
                getattr(getattr(self, '_loop', None), 'main_task_service', None)._current_exec_runtime_policy_payload()
                if callable(
                    getattr(
                        getattr(getattr(self, '_loop', None), 'main_task_service', None),
                        '_current_exec_runtime_policy_payload',
                        None,
                    )
                )
                else None
            ),
        )
        dynamic_appendix_messages = upsert_frontdoor_tool_contract_message(
            list(dynamic_appendix_messages),
            frontdoor_tool_contract,
        )

        live_messages = list(stable_messages)
        if not current_user_in_history:
            live_messages.append({"role": "user", "content": context_sources['user_content']})
        model_messages = [*live_messages, *list(dynamic_appendix_messages)]
        ephemeral_tail = self._prompt_message_records(ephemeral_tail_messages)
        if ephemeral_tail:
            model_messages.extend(ephemeral_tail)

        raw_history_messages = [
            dict(item)
            for item in list(stable_messages)
            if isinstance(item, dict)
            and str(item.get("role") or "").strip().lower() != "system"
        ]
        global_zone_source = list(raw_history_messages)
        pre_summary_messages = list(stable_messages)
        if not current_user_in_history:
            pre_summary_messages.append({"role": "user", "content": str(context_sources["user_content"] or "")})
        pre_summary_prompt_tokens = estimate_message_tokens(pre_summary_messages)
        compressed_zone_tokens = estimate_message_tokens(global_zone_source)
        global_summary_settings = self._global_summary_settings(self._loop)
        context_window_tokens = _resolve_ceo_context_window_tokens(self._loop)
        thresholds = build_global_summary_thresholds(
            context_window_tokens=context_window_tokens,
            compressed_zone_tokens=compressed_zone_tokens,
            trigger_ratio=global_summary_settings["trigger_ratio"],
            target_ratio=global_summary_settings["target_ratio"],
            min_output_tokens=global_summary_settings["min_output_tokens"],
            max_output_ratio=global_summary_settings["max_output_ratio"],
            max_output_tokens_ceiling=global_summary_settings["max_output_tokens_ceiling"],
            pressure_warn_ratio=global_summary_settings["pressure_warn_ratio"],
            force_refresh_ratio=global_summary_settings["force_refresh_ratio"],
        )
        semantic_state = {
            **default_semantic_context_state(),
            **dict(semantic_context_state or {}),
        }
        global_summary_text = str(semantic_state.get("summary_text") or "").strip()
        compression_state_payload = {
            "status": "ready" if global_summary_text else "",
            "text": "鍏ㄥ眬涓婁笅鏂囧凡鍘嬬缉" if global_summary_text else "",
            "source": "semantic" if global_summary_text else "",
            "needs_recheck": bool(semantic_state.get("needs_refresh")),
        }
        effective_prompt_tokens = estimate_message_tokens(model_messages)
        inject_elapsed_ms = self._elapsed_ms(inject_started_at)
        frontdoor_spans_ms = {
            'collect_context_sources': collect_elapsed_ms,
            'semantic_catalog_rankings': float((context_sources.get('span_timings_ms') or {}).get('semantic_catalog_rankings', 0.0) or 0.0),
            'retrieve_context_bundle': float((context_sources.get('span_timings_ms') or {}).get('retrieve_context_bundle', 0.0) or 0.0),
            'resolve_history_injection': history_elapsed_ms,
            'inject_turn_context': inject_elapsed_ms,
        }
        trace = {
            'selected_skills': list(context_sources['skill_trace']),
            'tool_selection': dict(context_sources['tool_trace']),
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
            'history_source': 'session_window',
            'checkpoint_message_count': len(stable_messages),
            'transcript_message_count': len(self._transcript_history(persisted_session)),
            'raw_history_message_count': len(raw_history_messages),
            'stage_workset_history_message_count': 0,
            'global_zone_message_count': len(global_zone_source),
            'global_zone_tokens': compressed_zone_tokens,
            'pre_summary_prompt_tokens': pre_summary_prompt_tokens,
            'effective_prompt_tokens': effective_prompt_tokens,
            'global_summary_trigger_tokens': int(thresholds["trigger_tokens"]),
            'global_summary_pressure_warn_tokens': int(thresholds["pressure_warn_tokens"]),
            'global_summary_force_refresh_tokens': int(thresholds["force_refresh_tokens"]),
            'global_summary_max_output_tokens': int(thresholds["max_output_tokens"]),
            'global_summary_present': False,
            'global_summary_trigger_reached': False,
            'global_summary_warn_reached': False,
            'global_summary_force_reached': False,
            'semantic_context_state': dict(semantic_state),
            'compression_state_payload': dict(compression_state_payload),
            'frontdoor_history_shrink_reason': '',
            'current_user_in_checkpoint': bool(current_user_in_history),
            'current_user_in_history': current_user_in_history,
            'current_user_in_transcript': self._transcript_has_current_user(
                persisted_session=persisted_session,
                query_text=query_text,
                user_metadata=user_metadata,
            ),
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
            'stable_prompt_split': False,
            'context_collection': {
                'retrieved_record_count': len(list(context_sources['retrieved_bundle'].records or [])),
                'retrieval_scope_mode': str(context_sources['retrieval_scope'].get('mode') or ''),
                'retrieved_context_present': bool(context_sources['retrieved_markdown']),
            },
            'message_injection': {
                'history_source': 'session_window',
                'history_message_count': len(stable_messages),
                'current_user_appended': not current_user_in_history,
                'retrieved_context_in_model_messages': bool(
                    context_sources['retrieved_markdown']
                ),
            },
            'frontdoor_spans_ms': frontdoor_spans_ms,
        }
        return ContextAssemblyResult(
            model_messages=model_messages,
            stable_messages=stable_messages,
            dynamic_appendix_messages=dynamic_appendix_messages,
            tool_names=list(context_sources['callable_tool_names']),
            candidate_tool_names=list(context_sources['selected_tool_names']),
            candidate_tool_items=list(context_sources.get('selected_tool_items') or []),
            trace=trace,
            turn_overlay_text=turn_overlay_text,
            cache_family_revision=(
                str(context_sources['capability_snapshot'].exposure_revision or '').strip()
                or DEFAULT_CACHE_FAMILY_REVISION
            ),
        )

    async def build_for_ceo(
        self,
        *,
        session: Any,
        query_text: str,
        exposure: dict[str, Any],
        persisted_session: Any | None,
        checkpoint_messages: list[dict[str, Any]] | None = None,
        request_body_seed_messages: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
        user_metadata: dict[str, Any] | None = None,
        frontdoor_stage_state: dict[str, Any] | None = None,
        frontdoor_canonical_context: dict[str, Any] | None = None,
        semantic_context_state: dict[str, Any] | None = None,
        hydrated_tool_names: list[str] | None = None,
        ephemeral_tail_messages: list[dict[str, Any]] | None = None,
    ) -> ContextAssemblyResult:
        collect_started_at = time.perf_counter()
        context_sources = await self._collect_turn_context_sources(
            session=session,
            query_text=query_text,
            exposure=exposure,
            user_content=user_content,
            hydrated_tool_names=hydrated_tool_names,
        )
        collect_elapsed_ms = self._elapsed_ms(collect_started_at)
        request_body_seed_records = self._request_body_seed_records(request_body_seed_messages)
        if request_body_seed_records:
            context_sources = {
                **dict(context_sources or {}),
                "frontdoor_stage_state": dict(frontdoor_stage_state or {}),
            }
            return self._build_direct_request_body_continuation_result(
                context_sources=context_sources,
                persisted_session=persisted_session,
                request_body_seed_messages=request_body_seed_records,
                query_text=query_text,
                user_metadata=user_metadata,
                semantic_context_state=semantic_context_state,
                ephemeral_tail_messages=ephemeral_tail_messages,
                collect_elapsed_ms=collect_elapsed_ms,
                history_elapsed_ms=0.0,
            )
        history_started_at = time.perf_counter()
        history_state = self._resolve_history_injection(
            persisted_session=persisted_session,
            checkpoint_messages=checkpoint_messages,
            query_text=query_text,
            user_metadata=user_metadata,
        )
        history_elapsed_ms = self._elapsed_ms(history_started_at)
        normalized_frontdoor_stage_state = dict(frontdoor_stage_state or {})
        normalized_frontdoor_canonical_context = normalize_frontdoor_canonical_context(
            frontdoor_canonical_context or {}
        )
        combined_frontdoor_context = combine_canonical_context(
            normalized_frontdoor_canonical_context,
            normalized_frontdoor_stage_state,
        )
        prompt_history_state = self._frontdoor_prompt_history(
            history_state=history_state,
            frontdoor_stage_state=combined_frontdoor_context,
            query_text=query_text,
            user_metadata=user_metadata,
        )
        raw_history_messages = list(prompt_history_state["history_messages"])
        effective_history_source = str(prompt_history_state["history_source"] or "").strip() or str(
            history_state["history_source"] or ""
        )
        current_user_in_history = bool(prompt_history_state["current_user_in_history"])
        retained_raw_stage_blocks, retained_completed_stage_ids = retained_raw_stage_messages(
            combined_frontdoor_context,
            keep_latest_completed_stages=3,
        )
        completed_blocks = completed_stage_blocks(
            combined_frontdoor_context,
            skip_stage_ids=retained_completed_stage_ids,
        )
        stage_workset_history = [*list(completed_blocks), *list(retained_raw_stage_blocks)]
        compression_blocks_for_summary = _externalized_completed_blocks_for_global_summary(completed_blocks)
        global_zone_source = [
            *list(compression_blocks_for_summary),
            *list(raw_history_messages),
            *self._hidden_internal_summary_messages(
                persisted_session=persisted_session,
                checkpoint_messages=checkpoint_messages,
            ),
        ]
        global_summary_settings = self._global_summary_settings(self._loop)
        context_window_tokens = _resolve_ceo_context_window_tokens(self._loop)
        pre_summary_messages = [
            {"role": "system", "content": str(context_sources["system_prompt"] or "")},
            *raw_history_messages,
            *stage_workset_history,
        ]
        if not current_user_in_history:
            pre_summary_messages.append({"role": "user", "content": str(context_sources["user_content"] or "")})
        pre_summary_prompt_tokens = estimate_message_tokens(pre_summary_messages)
        compressed_zone_tokens = estimate_message_tokens(global_zone_source)
        thresholds = build_global_summary_thresholds(
            context_window_tokens=context_window_tokens,
            compressed_zone_tokens=compressed_zone_tokens,
            trigger_ratio=global_summary_settings["trigger_ratio"],
            target_ratio=global_summary_settings["target_ratio"],
            min_output_tokens=global_summary_settings["min_output_tokens"],
            max_output_ratio=global_summary_settings["max_output_ratio"],
            max_output_tokens_ceiling=global_summary_settings["max_output_tokens_ceiling"],
            pressure_warn_ratio=global_summary_settings["pressure_warn_ratio"],
            force_refresh_ratio=global_summary_settings["force_refresh_ratio"],
        )
        semantic_state = {
            **default_semantic_context_state(),
            **dict(semantic_context_state or {}),
        }
        global_summary_text = str(semantic_state.get("summary_text") or "").strip()
        completed_stage_index = max(
            (
                int(item.get("stage_index") or 0)
                for item in list(combined_frontdoor_context.get("stages") or [])
                if isinstance(item, dict)
                and str(item.get("status") or "").strip().lower() != "active"
                and str(item.get("stage_id") or "").strip() not in retained_completed_stage_ids
            ),
            default=0,
        )
        refresh_decision = semantic_summary_refresh_decision(
            semantic_state=semantic_state,
            history_source=effective_history_source,
            prompt_tokens=pre_summary_prompt_tokens,
            trigger_tokens=int(thresholds["trigger_tokens"]),
            pressure_warn_tokens=int(thresholds["pressure_warn_tokens"]),
            force_refresh_tokens=int(thresholds["force_refresh_tokens"]),
            compressed_zone_tokens=compressed_zone_tokens,
            min_delta_tokens=int(global_summary_settings["min_delta_tokens"]),
            global_zone_message_count=len(global_zone_source),
            global_zone_stage_index=completed_stage_index,
        )
        trigger_reached = bool(refresh_decision["trigger_reached"])
        warn_reached = bool(refresh_decision["warn_reached"])
        force_reached = bool(refresh_decision["force_reached"])
        refresh_result = {
            "summary_text": global_summary_text,
            "used_fallback": False,
            "failed": False,
            "error_text": "",
        }
        summary_generated = False
        refresh_attempted = False
        if bool(refresh_decision["should_refresh"]):
            refresh_attempted = True
            refresh_result = normalize_summary_result(
                await summarize_global_context_model_first(
                    global_zone_source,
                    max_output_tokens=int(thresholds["max_output_tokens"] or global_summary_settings["min_output_tokens"]),
                    model_key=global_summary_settings["model_key"],
                )
            )
            refreshed_summary_text = str(refresh_result.get("summary_text") or "").strip()
            if refreshed_summary_text:
                global_summary_text = refreshed_summary_text
            if bool(refresh_result.get("failed")):
                semantic_state["failure_cooldown_until"] = future_cooldown_until(
                    seconds=int(global_summary_settings["failure_cooldown_seconds"]),
                )
            else:
                semantic_state["updated_at"] = self._now_iso()
                semantic_state["failure_cooldown_until"] = ""
                summary_generated = True
        elif bool(refresh_decision["cooldown_active"]) and not global_summary_text and global_zone_source:
            refresh_result = normalize_summary_result(
                await summarize_global_context_model_first(
                    global_zone_source,
                    max_output_tokens=int(thresholds["max_output_tokens"] or global_summary_settings["min_output_tokens"]),
                    model_key=global_summary_settings["model_key"],
                )
            )
            global_summary_text = str(refresh_result.get("summary_text") or "").strip()
            summary_generated = bool(global_summary_text)
        global_summary_message = (
            build_long_context_summary_message(global_summary_text)
            if str(global_summary_text or "").strip()
            else None
        )
        covered_count = len(raw_history_messages)
        semantic_state = {
            **default_semantic_context_state(),
            **semantic_state,
            "summary_text": global_summary_text,
            "coverage_history_source": effective_history_source,
            "coverage_message_index": max(-1, covered_count - 1),
            "coverage_stage_index": max(
                (
                    int(item.get("stage_index") or 0)
                    for item in list(combined_frontdoor_context.get("stages") or [])
                    if isinstance(item, dict)
                    and str(item.get("status") or "").strip().lower() != "active"
                ),
                default=0,
            ),
            "needs_refresh": bool(global_zone_source) and not warn_reached and compressed_zone_tokens >= int(global_summary_settings["min_delta_tokens"]),
        }
        compression_state_payload = {
            "status": "ready" if global_summary_message is not None else "idle",
            "text": "全局上下文已压缩" if global_summary_message is not None else "",
            "source": "semantic" if global_summary_message is not None else "",
            "needs_recheck": bool(semantic_state.get("needs_refresh")),
        }
        semantic_state["summary_text"] = global_summary_text
        if summary_generated:
            semantic_state["coverage_history_source"] = effective_history_source
            semantic_state["coverage_message_index"] = max(-1, len(global_zone_source) - 1)
            semantic_state["coverage_stage_index"] = completed_stage_index
        else:
            semantic_state["coverage_history_source"] = str(semantic_context_state.get("coverage_history_source") or "") if isinstance(semantic_context_state, dict) else ""
            semantic_state["coverage_message_index"] = int(
                semantic_context_state.get("coverage_message_index", -1) or -1
            ) if isinstance(semantic_context_state, dict) else -1
            semantic_state["coverage_stage_index"] = int(
                semantic_context_state.get("coverage_stage_index", 0) or 0
            ) if isinstance(semantic_context_state, dict) else 0
        semantic_state["needs_refresh"] = bool(refresh_decision["needs_refresh"]) or (
            refresh_attempted and bool(refresh_result.get("failed"))
        )
        compression_status = ""
        compression_text = ""
        compression_source = ""
        if global_summary_message is not None:
            compression_status = "ready"
            compression_text = "全局上下文已压缩"
            compression_source = "semantic"
        if refresh_attempted and bool(refresh_result.get("failed")):
            compression_status = "error"
            compression_text = "全局上下文压缩刷新失败，已回退到摘要缓存或回退摘要"
            compression_source = "semantic"
        compression_state_payload = {
            "status": compression_status,
            "text": compression_text,
            "source": compression_source,
            "needs_recheck": bool(semantic_state.get("needs_refresh")),
        }
        frontdoor_history_shrink_reason = ""
        if global_summary_message is not None:
            frontdoor_history_shrink_reason = "token_compression"
        elif completed_blocks:
            frontdoor_history_shrink_reason = "stage_compaction"
        staged_history_for_injection = [
            *([global_summary_message] if global_summary_message is not None else []),
            *raw_history_messages,
            *stage_workset_history,
        ]
        history_state['history_messages'] = staged_history_for_injection
        history_state['history_source'] = effective_history_source
        history_state['current_user_in_history'] = current_user_in_history
        task_ledger_state = self._task_ledger_state(persisted_session)
        task_ledger_text = build_task_ledger_summary(task_ledger_state)
        turn_overlay_parts = list(context_sources['turn_overlay_parts'])
        if task_ledger_text:
            insert_index = len(turn_overlay_parts)
            if str(context_sources['retrieved_markdown'] or '').strip():
                insert_index = max(0, insert_index - 1)
            turn_overlay_parts.insert(insert_index, task_ledger_text)
        inject_started_at = time.perf_counter()
        model_messages, stable_messages, dynamic_appendix_messages, turn_overlay_text = self._inject_turn_context(
            system_prompt=str(context_sources['system_prompt'] or ''),
            history_messages=list(history_state['history_messages']),
            user_content=context_sources['user_content'],
            split_prompt_builder=bool(context_sources['split_prompt_builder']),
            turn_overlay_parts=turn_overlay_parts,
            current_user_in_history=bool(history_state['current_user_in_history']),
        )
        frontdoor_tool_contract = build_frontdoor_tool_contract(
            callable_tool_names=list(context_sources['callable_tool_names']),
            candidate_tool_names=list(context_sources['selected_tool_names']),
            candidate_tool_items=list(context_sources.get('selected_tool_items') or []),
            hydrated_tool_names=list(context_sources['hydrated_tool_names']),
            frontdoor_stage_state=normalized_frontdoor_stage_state,
            visible_skill_ids=[
                self._skill_id(item)
                for item in list(context_sources['selected_skills'] or [])
                if self._skill_id(item)
            ],
            candidate_skill_ids=[
                self._skill_id(item)
                for item in list(context_sources['selected_skills'] or [])
                if self._skill_id(item)
            ],
            rbac_visible_tool_names=list(context_sources['capability_snapshot'].visible_tool_ids),
            rbac_visible_skill_ids=list(context_sources['capability_snapshot'].visible_skill_ids),
            contract_revision=(
                str(context_sources['capability_snapshot'].exposure_revision or '').strip()
                or DEFAULT_CACHE_FAMILY_REVISION
            ),
            exec_runtime_policy=(
                getattr(getattr(self, '_loop', None), 'main_task_service', None)._current_exec_runtime_policy_payload()
                if callable(
                    getattr(
                        getattr(getattr(self, '_loop', None), 'main_task_service', None),
                        '_current_exec_runtime_policy_payload',
                        None,
                    )
                )
                else None
            ),
        )
        dynamic_appendix_messages = upsert_frontdoor_tool_contract_message(
            list(dynamic_appendix_messages),
            frontdoor_tool_contract,
        )
        if bool(context_sources['split_prompt_builder']):
            model_messages = list(stable_messages)
        elif dynamic_appendix_messages:
            model_messages = [
                stable_messages[0],
                *dynamic_appendix_messages,
                *stable_messages[1:],
            ]
        else:
            model_messages = list(stable_messages)
        ephemeral_tail = self._prompt_message_records(ephemeral_tail_messages)
        if ephemeral_tail:
            model_messages.extend(ephemeral_tail)
        effective_prompt_tokens = estimate_message_tokens(model_messages)
        inject_elapsed_ms = self._elapsed_ms(inject_started_at)
        frontdoor_spans_ms = {
            'collect_context_sources': collect_elapsed_ms,
            'semantic_catalog_rankings': float((context_sources.get('span_timings_ms') or {}).get('semantic_catalog_rankings', 0.0) or 0.0),
            'retrieve_context_bundle': float((context_sources.get('span_timings_ms') or {}).get('retrieve_context_bundle', 0.0) or 0.0),
            'resolve_history_injection': history_elapsed_ms,
            'inject_turn_context': inject_elapsed_ms,
        }

        trace = {
            'selected_skills': list(context_sources['skill_trace']),
            'tool_selection': dict(context_sources['tool_trace']),
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
            'history_source': effective_history_source,
            'checkpoint_message_count': len(history_state['checkpoint_history']),
            'transcript_message_count': len(history_state['transcript_history']),
            'raw_history_message_count': len(raw_history_messages),
            'stage_workset_history_message_count': len(stage_workset_history),
            'global_zone_message_count': len(global_zone_source),
            'global_zone_tokens': compressed_zone_tokens,
            'pre_summary_prompt_tokens': pre_summary_prompt_tokens,
            'effective_prompt_tokens': effective_prompt_tokens,
            'global_summary_trigger_tokens': int(thresholds["trigger_tokens"]),
            'global_summary_pressure_warn_tokens': int(thresholds["pressure_warn_tokens"]),
            'global_summary_force_refresh_tokens': int(thresholds["force_refresh_tokens"]),
            'global_summary_max_output_tokens': int(thresholds["max_output_tokens"]),
            'global_summary_present': bool(global_summary_message),
            'global_summary_trigger_reached': bool(trigger_reached),
            'global_summary_warn_reached': bool(warn_reached),
            'global_summary_force_reached': bool(force_reached),
            'semantic_context_state': dict(semantic_state),
            'compression_state_payload': dict(compression_state_payload),
            'frontdoor_history_shrink_reason': frontdoor_history_shrink_reason,
            'current_user_in_checkpoint': bool(history_state['current_user_in_checkpoint']),
            'current_user_in_history': current_user_in_history,
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
                'history_source': effective_history_source,
                'history_message_count': len(history_state['history_messages']),
                'current_user_appended': not current_user_in_history,
                'retrieved_context_in_model_messages': bool(
                    context_sources['retrieved_markdown'] and not context_sources['split_prompt_builder']
                ),
            },
            'frontdoor_spans_ms': frontdoor_spans_ms,
        }
        return ContextAssemblyResult(
            model_messages=model_messages,
            stable_messages=stable_messages,
            dynamic_appendix_messages=dynamic_appendix_messages,
            tool_names=list(context_sources['callable_tool_names']),
            candidate_tool_names=list(context_sources['selected_tool_names']),
            candidate_tool_items=list(context_sources.get('selected_tool_items') or []),
            trace=trace,
            turn_overlay_text=turn_overlay_text,
            cache_family_revision=(
                str(context_sources['capability_snapshot'].exposure_revision or '').strip()
                or DEFAULT_CACHE_FAMILY_REVISION
            ),
        )
