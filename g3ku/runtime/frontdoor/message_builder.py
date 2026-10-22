from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from g3ku.agent.markdown_memory import split_consolidation_block
from g3ku.config.live_runtime import get_runtime_config
from g3ku.core.timefmt import (
    MESSAGE_ARRIVAL_TIME_MARKER,
    render_arrival_stamp,
    render_local_time,
    strip_arrival_time_stamp,
)
from g3ku.llm_config.runtime_resolver import resolve_chat_target
from g3ku.runtime.context.types import ContextAssemblyResult, RetrievedContextBundle
from g3ku.runtime.frontdoor.canonical_context import (
    combine_canonical_context,
    materialize_transcript_view,
    normalize_frontdoor_canonical_context,
)
from g3ku.runtime.frontdoor.capability_snapshot import CapabilitySnapshot, build_capability_snapshot
from g3ku.runtime.frontdoor.cron_hidden_prompt import strip_cron_hidden_prompt
from g3ku.runtime.frontdoor.prompt_cache_contract import DEFAULT_CACHE_FAMILY_REVISION
from g3ku.runtime.frontdoor.raw_stage_renderer import retained_raw_stage_messages
from g3ku.runtime.frontdoor.session_temp_dir import ceo_session_temp_dir
from g3ku.runtime.frontdoor.tool_contract import (
    build_frontdoor_tool_contract,
    frontdoor_pinned_contract_text,
    merge_pinned_contract_into_system_text,
    pinned_contract_is_carried_by_head,
    pinned_skill_ids_for,
    skill_roster_epoch_for,
    upsert_frontdoor_tool_contract_message,
)
from g3ku.runtime.message_token_estimation import estimate_message_tokens
from g3ku.runtime.stage_prompt_compaction import (
    compact_stage_prompt_messages_in_place,
    completed_stage_blocks,
    current_stage_active_window,
    keep_stage_blocks_off_continuation_tail,
    stage_prompt_prefix,
)
from g3ku.runtime.tool_visibility import (
    CEO_FIXED_BUILTIN_TOOL_NAMES,
)
from g3ku.runtime.web_ceo_sessions import (
    is_internal_ceo_user_message,
    is_prompt_visible_message,
    message_metadata,
    message_role,
    prompt_history_messages,
    trailing_turn_record,
    transcript_messages,
)
from main.runtime.tool_names import SILENT_TOOL_NAME

# 动态 overlay 块头（跨轮可比性投影按这些常量识别并剥离，禁止散文匹配）。
MEMORY_WRITE_HINT_HEADER = "## 长期记忆写入提示"
RETRIEVED_MEMORY_HINT_HEADER = "## 已检索记忆使用提示"

# 长期记忆快照的会话级冻结载体。快照注入在 message index 1（system 之后、整段历史之前），
# 所以它的字节变化等价于把身后全部历史重铺一遍；冻结值只在会话首请求采纳一次，
# 之后的变化由修订流增量投递（见 `skill_roster_notice` 的 `memory_cursor`）。
MEMORY_SNAPSHOT_TEXT_ATTR = "_frontdoor_memory_snapshot_text"
MEMORY_SNAPSHOT_ADOPTED_AT_ATTR = "_frontdoor_memory_snapshot_adopted_at"
MEMORY_SNAPSHOT_ADOPTION_REASON_ATTR = "_frontdoor_memory_snapshot_adoption_reason"
MEMORY_SNAPSHOT_ADOPTION_FIRST_REQUEST = "session_first_request"
# 压缩边界：上下文被整段重写，头部基座在这一跳更新到最新文档。
MEMORY_SNAPSHOT_ADOPTION_TOKEN_COMPRESSION = "token_compression"
MEMORY_SNAPSHOT_ADOPTION_MANUAL_COMPRESSION = "manual_compression"


def _read_memory_snapshot_text(session: Any, memory_manager: Any) -> str | None:
    """读当份文档快照；None 表示这次读取失败（与"文档确实为空"的 "" 区分开）。"""
    reader = getattr(memory_manager, "snapshot_text", None)
    if not callable(reader):
        return None
    session_key = str(getattr(getattr(session, "state", None), "session_key", "") or "")
    try:
        return str(
            reader(
                session_key=session_key,
                channel=str(getattr(session, "_memory_channel", getattr(session, "_channel", "cli")) or "cli"),
                chat_id=str(
                    getattr(session, "_memory_chat_id", getattr(session, "_chat_id", session_key)) or session_key
                ),
            )
            or ""
        ).strip()
    except Exception:
        return None


def adopt_memory_snapshot(session: Any, *, memory_manager: Any, reason: str) -> str:
    """采纳点：重读文档并覆盖会话冻结值。

    读取失败时保留上一次的冻结值且不盖时间戳——记忆 worker 与这里是并发的，
    一次瞬时读失败不该把整会话钉成空记忆。
    """
    text = _read_memory_snapshot_text(session, memory_manager)
    if text is None:
        return str(getattr(session, MEMORY_SNAPSHOT_TEXT_ATTR, "") or "")
    setattr(session, MEMORY_SNAPSHOT_TEXT_ATTR, text)
    setattr(session, MEMORY_SNAPSHOT_ADOPTED_AT_ATTR, datetime.now().astimezone().isoformat())
    setattr(session, MEMORY_SNAPSHOT_ADOPTION_REASON_ATTR, str(reason or "").strip())
    return text


def adopted_memory_snapshot_text(session: Any, *, memory_manager: Any) -> str:
    """注入侧唯一取数口：本会话已采纳过就只读冻结值，否则先采纳一次。"""
    frozen = getattr(session, MEMORY_SNAPSHOT_TEXT_ATTR, None)
    if isinstance(frozen, str):
        return frozen
    return adopt_memory_snapshot(session, memory_manager=memory_manager, reason=MEMORY_SNAPSHOT_ADOPTION_FIRST_REQUEST)


def memory_snapshot_provenance(session: Any) -> dict[str, Any]:
    """artifact 用的采纳溯源三元组。

    本地 prompt cache key 与 send-preflight 投影都不含记忆块，没有这组字段就无法验证
    冻结生效、也无法判读一条记忆还要多久才对当前会话可见。
    """
    text = getattr(session, MEMORY_SNAPSHOT_TEXT_ATTR, None)
    if not isinstance(text, str):
        return {}
    return {
        "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
        "adopted_at": str(getattr(session, MEMORY_SNAPSHOT_ADOPTED_AT_ATTR, "") or ""),
        "adoption_reason": str(getattr(session, MEMORY_SNAPSHOT_ADOPTION_REASON_ATTR, "") or ""),
    }


def _context_window_from_model_parameters(model_parameters: dict[str, Any] | None) -> int:
    payload = dict(model_parameters or {})
    value = payload.get("context_window_tokens")
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
    return 0


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
        'filesystem_stat': ('file', 'path', 'directory', 'size', 'measure', '目录', '清单', '大小'),
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
    # 与 RESERVED_INTERNAL_TOOLS 的区别只在一点：这些名字**不要求已经可见**，因此
    # 不依赖任何 tools/*/resource.yaml 声明族。RESERVED_INTERNAL_TOOLS 只保证
    # 「已可见时排在本轮 callable 最前面」，不具备注入能力 —— 照它加是加不进 schema 的。
    # 目前唯一的成员是静默收尾信号：它是前门的合同的一部分，不是可被选中的能力。
    ALWAYS_CALLABLE_INTERNAL_TOOLS: tuple[str, ...] = (SILENT_TOOL_NAME,)
    FIXED_BUILTIN_TOOL_NAMES: tuple[str, ...] = CEO_FIXED_BUILTIN_TOOL_NAMES
    ATTACHMENT_REOPEN_TARGET_LIMIT = 8

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
            record: dict[str, Any] = {
                "role": role,
                "content": content,
            }
            reasoning = raw.get("reasoning_content")
            if role == "assistant" and isinstance(reasoning, str) and reasoning.strip():
                record["reasoning_content"] = reasoning
            reasoning_items = [
                entry
                for entry in list(raw.get("reasoning_items") or [])
                if isinstance(entry, dict)
            ]
            if role == "assistant" and reasoning_items:
                record["reasoning_items"] = reasoning_items
            records.append(record)
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
            reasoning = raw.get("reasoning_content")
            if role == "assistant" and isinstance(reasoning, str) and reasoning.strip():
                record["reasoning_content"] = reasoning
            reasoning_items = [
                entry
                for entry in list(raw.get("reasoning_items") or [])
                if isinstance(entry, dict)
            ]
            if role == "assistant" and reasoning_items:
                record["reasoning_items"] = reasoning_items
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
                MEMORY_WRITE_HINT_HEADER,
                f'- 当前用户回合很可能在请求写入长期记忆（命中词：{terms}）。',
                '- 如果这是稳定的身份、偏好、约束、默认值、回避规则、工作流规则或项目长期事实，请在回复前调用 `memory_update`。',
                '- 不要把临时任务状态、猜测或未经确认的推断写入永久记忆。',
            ]
        )

    @staticmethod
    def _memory_snapshot_stable_message(memory_snapshot_text: str) -> dict[str, Any] | None:
        snapshot_text = str(memory_snapshot_text or '').strip()
        if not snapshot_text:
            return None
        entries_text, consolidation_text = split_consolidation_block(snapshot_text)
        # Both halves travel verbatim. The block used to be re-rendered here because the
        # painted mirror carried ids and `YYYY/M/D-source：` headers a reader never needed;
        # one line per memory prints neither, so a second rendering would only invent bytes
        # at message index 1 - the exact cache break the freeze exists to prevent.
        content = entries_text.strip()
        if consolidation_text:
            content = f"{content}\n{consolidation_text.strip()}".strip()
        if not content:
            return None
        return {
            "role": "assistant",
            "content": f"## 长期记忆\n{content}".strip(),
        }

    @staticmethod
    def _is_memory_snapshot_message(record: dict[str, Any] | None) -> bool:
        if not isinstance(record, dict):
            return False
        if str(record.get("role") or "").strip().lower() != "assistant":
            return False
        return str(record.get("content") or "").strip().startswith("## 长期记忆\n")

    @staticmethod
    def _retrieved_memory_resolution_hint_block() -> str:
        return '\n'.join(
            [
                RETRIEVED_MEMORY_HINT_HEADER,
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
    def _visible_tool_ids(cls, visible_tools: list[Any]) -> list[str]:
        ids: list[str] = []
        for item in list(visible_tools or []):
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
            visible_tools=list(exposure.get('tools') or []),
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
    def _callable_tool_names(
        cls,
        *,
        visible_tool_names: list[str],
        contract_present_tool_names: list[str] | None = None,
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
        for name in list(contract_present_tool_names or []):
            normalized = str(name or '').strip()
            if not normalized or normalized not in visible_set or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        # 追加而不是前插：既有 callable 顺序保持不变，渲染出的契约只在尾部多一项。
        # 与 _frontdoor_callable_tool_names_for_state 同一位置口径，避免同一个名字
        # 在两条路径上落在不同下标。
        for name in cls.ALWAYS_CALLABLE_INTERNAL_TOOLS:
            normalized = str(name or '').strip()
            if not normalized or normalized in seen:
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
        return CeoMessageBuilder._decorate_user_arrival_time(message, entry)

    @staticmethod
    def _decorate_user_arrival_time(message: Any, entry: dict[str, Any]) -> dict[str, Any]:
        """投影时给普通用户消息追加送达时间戳（持久化内容保持原文不动）。

        模型上下文原本没有任何"现在几点"的信息：system prompt 不含时间，转录的
        timestamp 字段也不进请求体，实测发生过模型凭上下文残片编造当前时间的事故
        （会话记录 21:41 却声称"现在 19:43"）。这里把每条用户消息**记录里已有的**
        timestamp 渲染成带时区偏移的可读时间追加到投影副本末尾——用记录固定值而非
        now()，保证跨回合字节稳定，不打断 provider 前缀缓存。

        heartbeat/cron 内部注入消息各自携带时间锚点（事件束头部唤醒时刻、cron
        送达时间字段），不重复装饰；已含标记的内容（如上一轮请求体基线回放的
        投影副本）跳过，保证幂等。
        """
        if entry.get('role') != 'user':
            return entry
        content = entry.get('content')
        if not isinstance(content, str) or not content.strip():
            return entry
        if MESSAGE_ARRIVAL_TIME_MARKER in content:
            return entry
        if is_internal_ceo_user_message(message):
            return entry
        timestamp = message.get('timestamp') if isinstance(message, dict) else getattr(message, 'timestamp', None)
        stamp = render_arrival_stamp(timestamp)
        if stamp:
            entry['content'] = content + stamp
        return entry

    def _transcript_history(self, persisted_session: Any | None) -> list[dict[str, Any]]:
        if persisted_session is None:
            return []
        return [
            self._history_message(message)
            for message in prompt_history_messages(persisted_session)
        ]

    def _checkpoint_history(self, checkpoint_messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        history = [
            self._history_message(message)
            for message in list(checkpoint_messages or [])
            if message_role(message) in {'user', 'assistant', 'tool'}
            and is_prompt_visible_message(message)
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

    # 内部规则行（心跳稳定规则 / cron 提醒正文）以 user 角色排在本轮 user 之前时，会冒充
    # "当前用户回合已在历史里"，事件束就此不再进请求体。跳过它们取真正的末位回合，
    # 判据与合同都归 web_ceo_sessions（内部提示词这条契约的持有者）。
    _trailing_turn_record = staticmethod(trailing_turn_record)

    def _history_has_current_user(
        self,
        *,
        history_messages: list[dict[str, Any]] | None,
        query_text: str,
        user_metadata: dict[str, Any] | None,
    ) -> bool:
        messages = [message for message in list(history_messages or []) if isinstance(message, dict)]
        last = self._trailing_turn_record(messages)
        if last is None:
            return False
        if str(last.get('role') or '').strip().lower() != 'user':
            return False
        last_metadata = last.get('metadata') if isinstance(last.get('metadata'), dict) else {}
        turn_id = str((user_metadata or {}).get('_transcript_turn_id') or '').strip()
        last_turn_id = str(last_metadata.get('_transcript_turn_id') or '').strip()
        if turn_id and last_turn_id:
            return last_turn_id == turn_id
        return self._cron_prompt_equal(last.get('content'), query_text)

    def _transcript_has_current_user(
        self,
        *,
        persisted_session: Any | None,
        query_text: str,
        user_metadata: dict[str, Any] | None,
    ) -> bool:
        if persisted_session is None:
            return False
        messages = [message for message in transcript_messages(persisted_session) if isinstance(message, dict)]
        last = self._trailing_turn_record(messages)
        if last is None:
            return False
        if str(last.get('role') or '').strip().lower() != 'user':
            return False
        last_metadata = last.get('metadata') if isinstance(last.get('metadata'), dict) else {}
        turn_id = str((user_metadata or {}).get('_transcript_turn_id') or '').strip()
        if turn_id and str(last_metadata.get('_transcript_turn_id') or '').strip() == turn_id:
            return True
        return self._cron_prompt_equal(last.get('content'), query_text)

    def _cron_prompt_equal(self, content: Any, query_text: str) -> bool:
        """比较当前用户内容时忽略 cron 隐藏契约与投影追加的送达时间戳。

        历史/转录里旧消息可能带宿主追加的隐藏契约，而新消息已剥除；直接
        全等比较会判失败，导致 frontdoor 没把当前用户置于末位（答非所问）。
        投影层给用户消息追加的 [消息送达时间] 装饰同理必须剥离后再比。
        """
        left = strip_arrival_time_stamp(strip_cron_hidden_prompt(self._content_text(content)).strip())
        right = strip_arrival_time_stamp(strip_cron_hidden_prompt(str(query_text or '')).strip())
        return left == right

    def _render_retrieved_context(self, bundle: RetrievedContextBundle) -> str:
        records = list(bundle.records or [])
        if not records:
            return ''
        lines = ['## 已检索上下文']
        for record in records:
            if not isinstance(record, dict):
                continue
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
    def _tool_record_map(visible_tools: list[Any]) -> dict[str, Any]:
        mapping: dict[str, Any] = {}
        for record in list(visible_tools or []):
            tool_id = str(getattr(record, "tool_id", "") or "").strip()
            if tool_id and tool_id not in mapping:
                mapping[tool_id] = record
        return mapping

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
    def _tool_intent_bonus(cls, *, tool_name: str, query_text: str) -> float:
        mutating_targets = cls._filesystem_intent_targets(query_text)
        if not mutating_targets:
            return 0.0
        normalized_tool_name = str(tool_name or '').strip()
        if normalized_tool_name == 'exec':
            return -8.0
        if normalized_tool_name in mutating_targets:
            return 12.0 - float(mutating_targets.index(normalized_tool_name))
        if normalized_tool_name.startswith('filesystem_'):
            return 3.0
        return 0.0

    def _candidate_tool_prompt_items(
        self,
        *,
        selected_tool_names: list[str],
        visible_tools: list[Any],
    ) -> list[dict[str, str]]:
        record_by_tool_id = self._tool_record_map(visible_tools)
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
                record = record_by_tool_id.get(tool_id)
                description = self._first_sentence(str(getattr(record, 'description', '') or '').strip()) if record is not None else ''
            items.append(
                {
                    'tool_id': tool_id,
                    'description': description,
                }
            )
        return items

    @staticmethod
    def _attachment_reopen_targets_from_uploads(
        uploads: Any,
    ) -> list[dict[str, str]]:
        items: list[dict[str, str]] = []
        for raw in list(uploads or []):
            if not isinstance(raw, dict):
                continue
            # 语音条不是"可以重开的附件"：它的内容已经在正文的转写里，而句柄一旦
            # 进契约就会每跳重复出现（历史消息也参与收集），既占满 8 条名额，
            # 又诱使模型去解码一个它读不了的音频文件。音频只服务气泡回放。
            if str(raw.get('kind') or '').strip().lower() == 'audio':
                continue
            path = str(raw.get('path') or '').strip()
            ref = str(raw.get('ref') or '').strip()
            if not path and not ref:
                continue
            item = {
                'name': str(raw.get('name') or path or ref).strip() or (path or ref),
                'kind': str(raw.get('kind') or '').strip(),
                'mime_type': str(raw.get('mime_type') or raw.get('mimeType') or '').strip(),
                'path': path,
                'ref': ref,
            }
            items.append({key: value for key, value in item.items() if value})
        return items

    @classmethod
    def _normalize_attachment_reopen_targets(
        cls,
        items: list[dict[str, str]] | None,
    ) -> list[dict[str, str]]:
        ordered: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in list(items or []):
            if not isinstance(raw, dict):
                continue
            path = str(raw.get('path') or '').strip()
            ref = str(raw.get('ref') or '').strip()
            if not path and not ref:
                continue
            dedupe_key = ref or path
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            ordered.append(
                {
                    key: value
                    for key, value in {
                        'name': str(raw.get('name') or path or ref).strip() or (path or ref),
                        'kind': str(raw.get('kind') or '').strip(),
                        'mime_type': str(raw.get('mime_type') or '').strip(),
                        'path': path,
                        'ref': ref,
                    }.items()
                    if value
                }
            )
            if len(ordered) >= cls.ATTACHMENT_REOPEN_TARGET_LIMIT:
                break
        return ordered

    @classmethod
    def _frontdoor_attachment_reopen_targets(
        cls,
        *,
        persisted_session: Any | None,
        user_metadata: dict[str, Any] | None,
    ) -> list[dict[str, str]]:
        collected: list[dict[str, str]] = []
        current_uploads = (
            user_metadata.get('web_ceo_uploads')
            if isinstance(user_metadata, dict)
            else None
        )
        collected.extend(cls._attachment_reopen_targets_from_uploads(current_uploads))
        if persisted_session is not None:
            for message in reversed(transcript_messages(persisted_session)):
                metadata = message_metadata(message)
                collected.extend(cls._attachment_reopen_targets_from_uploads(metadata.get('web_ceo_uploads')))
        return cls._normalize_attachment_reopen_targets(collected)

    @staticmethod
    def _now_iso() -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _normalize_hidden_internal_summary_message(
        message: dict[str, Any],
        *,
        source_rows: list[Any] | None = None,
        row_index: int = -1,
    ) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return None
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
        if not canonical_context and isinstance(message.get("cc_upsert"), dict) and source_rows is not None:
            # delta 存储行只带当轮 upsert；进 prompt 的内部摘要需要该行自己的累积视图。
            canonical_context = materialize_transcript_view(source_rows, row_index)
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
        persisted_rows = list(getattr(persisted_session, "messages", []) or []) if persisted_session is not None else []
        for row_index, raw in enumerate(persisted_rows):
            if isinstance(raw, dict):
                normalized = cls._normalize_hidden_internal_summary_message(
                    raw,
                    source_rows=persisted_rows,
                    row_index=row_index,
                )
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
        if not isinstance(record, dict):
            return False
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

    @staticmethod
    def _tool_is_registered_callable(record: Any, visible_name_set: set[str]) -> bool:
        return bool(str(getattr(record, 'tool_id', '') or '').strip() in visible_name_set)

    def _trace_external_tools(
        self,
        *,
        visible_tools: list[Any],
        visible_tool_names: list[str],
    ) -> list[dict[str, Any]]:
        visible_name_set = {
            str(name or '').strip()
            for name in list(visible_tool_names or [])
            if str(name or '').strip()
        }
        trace: list[dict[str, Any]] = []
        for record in list(visible_tools or []):
            tool_id = str(getattr(record, 'tool_id', '') or '').strip()
            callable_flag = bool(getattr(record, 'callable', True))
            available_flag = bool(getattr(record, 'available', True))
            registered_callable = self._tool_is_registered_callable(record, visible_name_set)
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

    @staticmethod
    def _visible_only_skill_trace(visible_skills: list[Any]) -> list[dict[str, Any]]:
        return [
            {'skill_id': CeoMessageBuilder._skill_id(item)}
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
        contract_present_tool_names: list[str] | None = None,
    ) -> dict[str, Any]:
        retrieval_started_at = 0.0
        retrieval_elapsed_ms = 0.0
        main_service = getattr(self._loop, 'main_task_service', None)
        memory_manager = getattr(self._loop, 'memory_manager', None)
        visible_tool_names = [
            str(name or '').strip()
            for name in list(exposure.get('tool_names') or [])
            if str(name or '').strip()
        ]
        callable_tool_names = self._callable_tool_names(
            visible_tool_names=visible_tool_names,
            contract_present_tool_names=list(contract_present_tool_names or []),
        )
        callable_tool_name_set = set(callable_tool_names)

        if main_service is not None and memory_manager is not None:
            try:
                maybe_refresh = getattr(main_service, 'maybe_refresh_external_resource_changes', None)
                if callable(maybe_refresh):
                    await maybe_refresh(
                        session_id=str(getattr(getattr(session, 'state', None), 'session_key', '') or 'web:shared').strip() or 'web:shared',
                    )
            except Exception:
                pass

        memory_write_terms = self._detect_memory_write_intent(query_text)
        visible_skills = list(exposure.get('skills') or [])
        visible_tools = list(exposure.get('tools') or [])
        capability_snapshot = self._capability_snapshot(exposure)
        # 名单与候选都不按 query 排序：可见集原样给全，"选哪个"由模型按描述判断，
        # 收窄由执行准入与 load 承担。skill 侧头部钉住块带全量可见名单（id|名称|描述）。
        selected_skills = self._visible_only_skill_items(visible_skills)
        skill_trace = self._visible_only_skill_trace(selected_skills)
        split_prompt_builder = callable(getattr(self._prompt_builder, 'build_base_prompt', None))
        if split_prompt_builder:
            # 提示词的重新拉取跟着记忆块与 tools[] 的时钟走：这两个戳没动，稳定前缀就不该
            # 出现新的字节；会话中途改写的提示词文件因此等到下一次采纳/曝光变化才生效。
            prompt_refresh_stamp = (
                f"{str(getattr(session, MEMORY_SNAPSHOT_ADOPTED_AT_ATTR, '') or '').strip()}"
                f"|{str(getattr(capability_snapshot, 'exposure_revision', '') or '').strip()}"
            )
            system_prompt = self._prompt_builder.build_base_prompt(cache_stamp=prompt_refresh_stamp)
        else:
            system_prompt = self._prompt_builder.build(skills=[])
        if capability_snapshot.stable_catalog_message:
            system_prompt = f"{system_prompt}\n\n{capability_snapshot.stable_catalog_message}".strip()
        external_trace = self._trace_external_tools(
            visible_tools=visible_tools,
            visible_tool_names=visible_tool_names,
        )

        memory_write_visible = 'memory_update' in {
            str(name or '').strip()
            for name in visible_tool_names
            if str(name or '').strip()
        }
        turn_overlay_parts: list[str] = []
        memory_snapshot_text = (
            adopted_memory_snapshot_text(session, memory_manager=memory_manager)
            if memory_manager is not None
            else ""
        )
        if memory_write_terms and memory_write_visible:
            turn_overlay_parts.append(self._memory_write_hint_block(memory_write_terms))

        selected_tool_names = []
        seen_tool_names: set[str] = set()
        for name in visible_tool_names:
            normalized = str(name or '').strip()
            if not normalized or normalized in seen_tool_names:
                continue
            seen_tool_names.add(normalized)
            selected_tool_names.append(normalized)
        tool_trace = {
            'mode': 'inventory_only',
            'reserved_internal_tool_names': [
                name for name in selected_tool_names if name in self.RESERVED_INTERNAL_TOOLS
            ],
            'fixed_builtin_tool_names': [
                name
                for name in selected_tool_names
                if name in self.FIXED_BUILTIN_TOOL_NAMES
            ],
            'contract_present_tool_names': [
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
        candidate_tool_names = [
            str(name or '').strip()
            for name in list(tool_trace.get('candidate_tool_names') or [])
            if str(name or '').strip()
        ]
        main_service = getattr(self._loop, 'main_task_service', None)
        if callable(getattr(main_service, '_split_repair_required_tool_prompt_items', None)):
            candidate_tool_names, candidate_tool_items, repair_required_tool_items = getattr(
                main_service,
                '_split_repair_required_tool_prompt_items',
            )(
                candidate_tool_names=list(candidate_tool_names),
                visible_tool_records=visible_tools,
                preferred_items=None,
            )
        else:
            candidate_tool_items = self._candidate_tool_prompt_items(
                selected_tool_names=list(candidate_tool_names),
                visible_tools=visible_tools,
            )
            repair_required_tool_items = []
        if callable(getattr(main_service, '_all_repair_required_tool_prompt_items', None)):
            repair_required_tool_items = getattr(main_service, '_merge_repair_prompt_items')(
                repair_required_tool_items,
                getattr(main_service, '_all_repair_required_tool_prompt_items')(
                    visible_tool_records=visible_tools,
                ),
                key='tool_id',
            )
        repair_required_tool_ids = {
            str(item.get('tool_id') or '').strip()
            for item in list(repair_required_tool_items or [])
            if str(item.get('tool_id') or '').strip()
        }
        candidate_tool_names = [
            name
            for name in list(candidate_tool_names or [])
            if name not in repair_required_tool_ids
        ]
        candidate_tool_items = [
            dict(item)
            for item in list(candidate_tool_items or [])
            if str(item.get('tool_id') or '').strip() not in repair_required_tool_ids
        ]
        candidate_skill_ids = [
            self._skill_id(item)
            for item in list(selected_skills or [])
            if self._skill_id(item)
        ]
        if callable(getattr(main_service, '_split_repair_required_skill_prompt_items', None)):
            candidate_skill_ids, candidate_skill_items, repair_required_skill_items = getattr(
                main_service,
                '_split_repair_required_skill_prompt_items',
            )(
                candidate_skill_ids=list(candidate_skill_ids),
                visible_skills=visible_skills,
                preferred_items=None,
            )
            selected_skills = [
                item
                for item in list(selected_skills or [])
                if self._skill_id(item) in set(candidate_skill_ids)
            ]
            selected_skill_id_set = set(candidate_skill_ids)
            skill_trace = [
                item
                for item in list(skill_trace or [])
                if str((item or {}).get('skill_id') or '').strip() in selected_skill_id_set
            ]
        else:
            candidate_skill_items = []
            repair_required_skill_items = []
        if callable(getattr(main_service, '_all_repair_required_skill_prompt_items', None)):
            repair_required_skill_items = getattr(main_service, '_merge_repair_prompt_items')(
                repair_required_skill_items,
                getattr(main_service, '_all_repair_required_skill_prompt_items')(
                    visible_skills=visible_skills,
                ),
                key='skill_id',
            )
        repair_required_skill_ids = {
            str(item.get('skill_id') or '').strip()
            for item in list(repair_required_skill_items or [])
            if str(item.get('skill_id') or '').strip()
        }
        candidate_skill_ids = [
            skill_id
            for skill_id in list(candidate_skill_ids or [])
            if skill_id not in repair_required_skill_ids
        ]
        candidate_skill_items = [
            dict(item)
            for item in list(candidate_skill_items or [])
            if str(item.get('skill_id') or '').strip() not in repair_required_skill_ids
        ]
        selected_skills = [
            item
            for item in list(selected_skills or [])
            if self._skill_id(item) not in repair_required_skill_ids
        ]
        skill_trace = [
            item
            for item in list(skill_trace or [])
            if str((item or {}).get('skill_id') or '').strip() not in repair_required_skill_ids
        ]
        retrieved_bundle = RetrievedContextBundle(query=query_text)
        retrieval_scope = {
            'mode': 'disabled',
            'search_context_types': [],
            'allowed_context_types': [],
            'allowed_resource_record_ids': [],
            'allowed_skill_record_ids': [],
        }
        retrieved_markdown = ""
        same_session_turn_memory_filtered_count = 0
        retrieval_elapsed_ms = 0.0

        return {
            'capability_snapshot': capability_snapshot,
            'memory_write_terms': memory_write_terms,
            'memory_write_visible': memory_write_visible,
            'memory_snapshot_text': memory_snapshot_text,
            'selected_skills': selected_skills,
            'skill_trace': skill_trace,
            'external_trace': external_trace,
            'selected_tool_names': candidate_tool_names,
            'selected_tool_items': candidate_tool_items,
            'candidate_skill_items': candidate_skill_items,
            'repair_required_tool_items': repair_required_tool_items,
            'repair_required_skill_items': repair_required_skill_items,
            'callable_tool_names': callable_tool_names,
            'contract_present_tool_names': [
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

    @staticmethod
    def _current_user_content_with_arrival_time(user_content: Any) -> Any:
        """当前回合用户消息不在历史里、直接追加进请求时，用 now() 盖送达时间戳。

        常规路径下用户消息先持久化再投影（装饰来自记录 timestamp，跨回合字节稳定）；
        这里只覆盖"记录尚不存在"的兜底路径，下一回合起装饰会自动切换为记录时间。
        非字符串内容（多模态块）原样返回。
        """
        if not isinstance(user_content, str):
            return user_content
        if not user_content.strip() or MESSAGE_ARRIVAL_TIME_MARKER in user_content:
            return user_content
        return f"{user_content}\n\n{MESSAGE_ARRIVAL_TIME_MARKER} {render_local_time()}"

    def _frontdoor_exec_runtime_policy(self) -> dict[str, Any] | None:
        main_service = getattr(self._loop, 'main_task_service', None)
        getter = getattr(main_service, '_current_exec_runtime_policy_payload', None)
        if not callable(getter):
            return None
        try:
            return getter()
        except Exception:
            return None

    def _frontdoor_session_key(self, persisted_session: Any) -> str:
        return str(getattr(getattr(persisted_session, 'state', None), 'session_key', '') or '').strip()

    def _frontdoor_pinned_contract(
        self,
        *,
        persisted_session: Any,
        context_sources: dict[str, Any],
    ) -> tuple[str, list[str]]:
        """钉进头部的静态声明原文 + 它钉住的那份名单（空串＝本轮不钉，尾块照旧带全量）。

        判据键与 `prompt contract` 那条装配路必须逐字一致，否则两边各自重印、头部每跳都变，
        两侧共用 `tool_contract` 里的同一张进程表。
        """
        session_key = self._frontdoor_session_key(persisted_session)
        pinned_text = frontdoor_pinned_contract_text(
            persisted_session,
            skill_items=list(context_sources.get('selected_skills') or []),
            exec_runtime_policy=self._frontdoor_exec_runtime_policy(),
            session_temp_dir=ceo_session_temp_dir(session_key),
            skill_roster_epoch=skill_roster_epoch_for(persisted_session, session_key=session_key),
            session_key=session_key,
        )
        if not pinned_text:
            return '', []
        return pinned_text, pinned_skill_ids_for(persisted_session, session_key=session_key)

    def _inject_turn_context(
        self,
        *,
        system_prompt: str,
        history_messages: list[dict[str, Any]],
        user_content: Any,
        split_prompt_builder: bool,
        turn_overlay_parts: list[str],
        memory_snapshot_text: str,
        current_user_in_history: bool,
        pinned_contract_text: str = '',
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str]:
        turn_overlay_text = self._join_turn_overlay_sections(turn_overlay_parts)
        system_prompt = merge_pinned_contract_into_system_text(system_prompt, pinned_contract_text)
        stable_messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        memory_snapshot_message = self._memory_snapshot_stable_message(memory_snapshot_text)
        if memory_snapshot_message is not None:
            stable_messages.append(memory_snapshot_message)
        stable_messages.extend(history_messages)
        has_user_content = bool(self._content_text(user_content).strip())
        if not current_user_in_history and has_user_content:
            stable_messages.append(
                {
                    "role": "user",
                    "content": self._current_user_content_with_arrival_time(user_content),
                }
            )
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
        memory_snapshot_text: str,
        query_text: str,
        user_metadata: dict[str, Any] | None,
        pinned_contract_text: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], str, bool]:
        turn_overlay_text = self._join_turn_overlay_sections(turn_overlay_parts)
        stable_messages = [
            dict(item)
            for item in self._request_body_seed_records(request_body_seed_messages)
            if not self._is_memory_snapshot_message(item)
        ]
        memory_snapshot_message = self._memory_snapshot_stable_message(memory_snapshot_text)
        if memory_snapshot_message is not None:
            insert_at = 1 if stable_messages and str(stable_messages[0].get("role") or "").strip().lower() == "system" else 0
            stable_messages = [
                *list(stable_messages[:insert_at]),
                memory_snapshot_message,
                *list(stable_messages[insert_at:]),
            ]
        # 钉住的静态声明并进 system 文本尾部：它不是消息项，因此尾部重铺那条剥契约的路
        # 碰不到它；字节只在刷新键变化时改一次。这条路的头部来自上一跳的请求体种子，已经带着
        # 上一跳那份块，必须先摘再拼——直接 concat 会让续跑回合每轮多叠一份（实盘 06:15 抓到）。
        pinned_text = str(pinned_contract_text or "").strip()
        if pinned_text and stable_messages and str(stable_messages[0].get("role") or "").strip().lower() == "system":
            merged_first = dict(stable_messages[0])
            merged_first["content"] = merge_pinned_contract_into_system_text(
                merged_first.get("content"),
                pinned_text,
            )
            stable_messages = [merged_first, *list(stable_messages[1:])]
        current_user_in_history = self._history_has_current_user(
            history_messages=stable_messages,
            query_text=query_text,
            user_metadata=user_metadata,
        )
        live_messages = list(stable_messages)
        has_user_content = bool(self._content_text(user_content).strip())
        dynamic_appendix_messages = [
            {"role": "assistant", "content": part}
            for part in turn_overlay_parts
            if str(part or "").strip()
        ]
        # overlay/契约放在当前 user 之前，保证模型最后看到的是用户回合，
        # 而不是把工具契约/overlay 当“上一条发言”回显。
        model_messages = [*live_messages, *dynamic_appendix_messages]
        if not current_user_in_history and has_user_content:
            model_messages.append(
                {
                    "role": "user",
                    "content": self._current_user_content_with_arrival_time(user_content),
                }
            )
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
        attachment_reopen_targets = self._frontdoor_attachment_reopen_targets(
            persisted_session=persisted_session,
            user_metadata=user_metadata,
        )
        turn_overlay_parts = list(context_sources['turn_overlay_parts'])

        pinned_contract_text, pinned_skill_ids = self._frontdoor_pinned_contract(
            persisted_session=persisted_session,
            context_sources=context_sources,
        )

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
            memory_snapshot_text=str(context_sources.get('memory_snapshot_text') or ''),
            query_text=query_text,
            user_metadata=user_metadata,
            pinned_contract_text=pinned_contract_text,
        )
        # 这条路的头部来自上一跳的请求体种子，不保证首条仍是 system；没并进就不许尾块省略。
        if not pinned_contract_is_carried_by_head(stable_messages, pinned_contract_text):
            pinned_contract_text, pinned_skill_ids = '', []

        frontdoor_tool_contract = build_frontdoor_tool_contract(
            pinned_contract_text=pinned_contract_text,
            pinned_skill_ids=pinned_skill_ids,
            callable_tool_names=list(context_sources['callable_tool_names']),
            candidate_tool_names=list(context_sources['selected_tool_names']),
            candidate_tool_items=list(context_sources.get('selected_tool_items') or []),
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
            candidate_skill_items=list(context_sources.get('candidate_skill_items') or []),
            repair_required_tool_items=list(context_sources.get('repair_required_tool_items') or []),
            repair_required_skill_items=list(context_sources.get('repair_required_skill_items') or []),
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
            attachment_reopen_targets=attachment_reopen_targets,
        )
        dynamic_appendix_messages = upsert_frontdoor_tool_contract_message(
            list(dynamic_appendix_messages),
            frontdoor_tool_contract,
        )

        live_messages = list(stable_messages)
        has_user_content = bool(self._content_text(context_sources['user_content']).strip())
        model_messages = [*live_messages, *list(dynamic_appendix_messages)]
        ephemeral_tail = self._prompt_message_records(ephemeral_tail_messages)
        if ephemeral_tail:
            model_messages.extend(ephemeral_tail)
        # 当前用户回合必须是模型看到的最后一条，避免契约/overlay 占据末位被回显。
        # 装饰只能有一个作者：另两条道都盖送达时间（1900/1975），这里漏了就等于续跑轮
        # 的模型看不到"这条问题是什么时候到的"。估算与发送体也共用同一条消息。
        current_user_message = None
        if not current_user_in_history and has_user_content:
            current_user_message = {
                "role": "user",
                "content": self._current_user_content_with_arrival_time(context_sources['user_content']),
            }
            model_messages.append(current_user_message)

        raw_history_messages = [
            dict(item)
            for item in list(stable_messages)
            if isinstance(item, dict)
            and str(item.get("role") or "").strip().lower() != "system"
        ]
        history_zone_source = list(raw_history_messages)
        pre_request_messages = list(stable_messages)
        if current_user_message is not None:
            pre_request_messages.append(dict(current_user_message))
        pre_request_prompt_tokens = estimate_message_tokens(pre_request_messages)
        history_zone_tokens = estimate_message_tokens(history_zone_source)
        compression_state_payload = {
            "status": "",
            "text": "",
            "source": "",
            "needs_recheck": False,
        }
        effective_prompt_tokens = estimate_message_tokens(model_messages)
        inject_elapsed_ms = self._elapsed_ms(inject_started_at)
        frontdoor_spans_ms = {
            'collect_context_sources': collect_elapsed_ms,
            'retrieve_context_bundle': float((context_sources.get('span_timings_ms') or {}).get('retrieve_context_bundle', 0.0) or 0.0),
            'resolve_history_injection': history_elapsed_ms,
            'inject_turn_context': inject_elapsed_ms,
        }
        trace = {
            'selected_skills': list(context_sources['skill_trace']),
            'tool_selection': dict(context_sources['tool_trace']),
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
            'history_zone_message_count': len(history_zone_source),
            'history_zone_tokens': history_zone_tokens,
            'pre_request_prompt_tokens': pre_request_prompt_tokens,
            'effective_prompt_tokens': effective_prompt_tokens,
            'compression_state_payload': dict(compression_state_payload),
            'frontdoor_history_shrink_reason': '',
            'current_user_in_checkpoint': bool(current_user_in_history),
            'current_user_in_history': current_user_in_history,
            'current_user_in_transcript': self._transcript_has_current_user(
                persisted_session=persisted_session,
                query_text=query_text,
                user_metadata=user_metadata,
            ),
            'retrieved_record_count': len(list(context_sources['retrieved_bundle'].records or [])),
            'same_session_turn_memory_filtered_count': int(context_sources['same_session_turn_memory_filtered_count']),
            'model_messages_count': len(model_messages),
            'stable_prefix_message_count': len(stable_messages),
            'dynamic_appendix_message_count': len(dynamic_appendix_messages),
            'turn_overlay_present': bool(turn_overlay_text),
            'turn_overlay_section_count': len(turn_overlay_parts),
            'turn_overlay_character_count': len(turn_overlay_text),
            'attachment_reopen_targets': list(attachment_reopen_targets),
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
            repair_required_tool_items=list(context_sources.get('repair_required_tool_items') or []),
            repair_required_skill_items=list(context_sources.get('repair_required_skill_items') or []),
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
        internal_seed_messages: list[dict[str, Any]] | None = None,
        user_content: Any | None = None,
        user_metadata: dict[str, Any] | None = None,
        frontdoor_stage_state: dict[str, Any] | None = None,
        frontdoor_canonical_context: dict[str, Any] | None = None,
        semantic_context_state: dict[str, Any] | None = None,
        contract_present_tool_names: list[str] | None = None,
        ephemeral_tail_messages: list[dict[str, Any]] | None = None,
    ) -> ContextAssemblyResult:
        collect_started_at = time.perf_counter()
        context_sources = await self._collect_turn_context_sources(
            session=session,
            query_text=query_text,
            exposure=exposure,
            user_content=user_content,
            contract_present_tool_names=contract_present_tool_names,
        )
        collect_elapsed_ms = self._elapsed_ms(collect_started_at)

        attachment_reopen_targets = self._frontdoor_attachment_reopen_targets(
            persisted_session=persisted_session,
            user_metadata=user_metadata,
        )
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
        # 冷启动新建路径：内部事件消息（cron/heartbeat）不进入续跑种子，改在这里
        # 注入历史区，保证基础系统提示由 _inject_turn_context 正常前置。
        internal_seed_records = self._request_body_seed_records(internal_seed_messages)
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
        )
        completed_blocks = completed_stage_blocks(
            combined_frontdoor_context,
            skip_stage_ids=retained_completed_stage_ids,
        )
        # 双模式裁剪：含模型内容的历史走"按阶段归属原位压缩"（过期阶段只删工具肉身、
        # 块落回原位、对话保留）；checkpoint 路径 raw 只剩 user，维持 workset 拼装。
        lead_prefix, raw_remainder = stage_prompt_prefix(
            raw_history_messages,
            preserve_leading_system=False,
            preserve_leading_user=True,
        )
        raw_has_model_content = any(
            str((item or {}).get("role") or "").strip().lower() in ("assistant", "tool")
            for item in raw_remainder
        )
        if raw_has_model_content:
            in_place_parts = compact_stage_prompt_messages_in_place(
                raw_history_messages,
                stage_state=combined_frontdoor_context,
                preserve_leading_system=False,
                preserve_leading_user=True,
            )
            trimmed_raw_history = [*list(in_place_parts["prefix"]), *list(in_place_parts["rewritten"])]
            stage_workset_history: list[dict[str, Any]] = []
            stage_compaction_applied = bool(in_place_parts.get("stage_compaction_applied"))
        else:
            raw_active_window = current_stage_active_window(
                raw_remainder,
                keep_completed_stages=0,
            )
            trimmed_raw_history = [*lead_prefix, *raw_active_window]
            stage_workset_history = [*list(completed_blocks), *list(retained_raw_stage_blocks)]
            stage_compaction_applied = bool(completed_blocks)
        # 不变量守卫：块不占续写位。当前用户回合已在历史里时，workset 块/回插块
        # 不得落在其后方——落在最后一条 user 之后的阶段块整体前移到该 user 之前
        # （见 keep_stage_blocks_off_continuation_tail），请求末位保持用户回合。
        staged_stage_history = keep_stage_blocks_off_continuation_tail(
            [*list(trimmed_raw_history), *list(stage_workset_history)]
        )
        history_zone_source = [
            *list(staged_stage_history),
            *self._hidden_internal_summary_messages(
                persisted_session=persisted_session,
                checkpoint_messages=checkpoint_messages,
            ),
            *internal_seed_records,
        ]
        pre_request_messages = [
            {"role": "system", "content": str(context_sources["system_prompt"] or "")},
            *staged_stage_history,
            *internal_seed_records,
        ]
        if not current_user_in_history:
            pre_request_messages.append({"role": "user", "content": str(context_sources["user_content"] or "")})
        pre_request_prompt_tokens = estimate_message_tokens(pre_request_messages)
        history_zone_tokens = estimate_message_tokens(history_zone_source)
        compression_state_payload = {
            "status": "",
            "text": "",
            "source": "",
            "needs_recheck": False,
        }
        frontdoor_history_shrink_reason = "stage_compaction" if stage_compaction_applied else ""
        staged_history_for_injection = [
            *staged_stage_history,
            *internal_seed_records,
        ]
        history_state['history_messages'] = staged_history_for_injection
        history_state['history_source'] = effective_history_source
        history_state['current_user_in_history'] = current_user_in_history
        turn_overlay_parts = list(context_sources['turn_overlay_parts'])
        pinned_contract_text, pinned_skill_ids = self._frontdoor_pinned_contract(
            persisted_session=persisted_session,
            context_sources=context_sources,
        )
        inject_started_at = time.perf_counter()
        model_messages, stable_messages, dynamic_appendix_messages, turn_overlay_text = self._inject_turn_context(
            system_prompt=str(context_sources['system_prompt'] or ''),
            history_messages=list(history_state['history_messages']),
            user_content=context_sources['user_content'],
            split_prompt_builder=bool(context_sources['split_prompt_builder']),
            turn_overlay_parts=turn_overlay_parts,
            memory_snapshot_text=str(context_sources.get('memory_snapshot_text') or ''),
            current_user_in_history=bool(history_state['current_user_in_history']),
            pinned_contract_text=pinned_contract_text,
        )
        frontdoor_tool_contract = build_frontdoor_tool_contract(
            pinned_contract_text=pinned_contract_text,
            pinned_skill_ids=pinned_skill_ids,
            callable_tool_names=list(context_sources['callable_tool_names']),
            candidate_tool_names=list(context_sources['selected_tool_names']),
            candidate_tool_items=list(context_sources.get('selected_tool_items') or []),
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
            candidate_skill_items=list(context_sources.get('candidate_skill_items') or []),
            repair_required_tool_items=list(context_sources.get('repair_required_tool_items') or []),
            repair_required_skill_items=list(context_sources.get('repair_required_skill_items') or []),
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
            attachment_reopen_targets=attachment_reopen_targets,
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
            'retrieve_context_bundle': float((context_sources.get('span_timings_ms') or {}).get('retrieve_context_bundle', 0.0) or 0.0),
            'resolve_history_injection': history_elapsed_ms,
            'inject_turn_context': inject_elapsed_ms,
        }

        trace = {
            'selected_skills': list(context_sources['skill_trace']),
            'tool_selection': dict(context_sources['tool_trace']),
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
            'history_zone_message_count': len(history_zone_source),
            'history_zone_tokens': history_zone_tokens,
            'pre_request_prompt_tokens': pre_request_prompt_tokens,
            'effective_prompt_tokens': effective_prompt_tokens,
            'compression_state_payload': dict(compression_state_payload),
            'frontdoor_history_shrink_reason': frontdoor_history_shrink_reason,
            'current_user_in_checkpoint': bool(history_state['current_user_in_checkpoint']),
            'current_user_in_history': current_user_in_history,
            'current_user_in_transcript': bool(history_state['current_user_in_transcript']),
            'retrieved_record_count': len(list(context_sources['retrieved_bundle'].records or [])),
            'same_session_turn_memory_filtered_count': int(context_sources['same_session_turn_memory_filtered_count']),
            'model_messages_count': len(model_messages),
            'stable_prefix_message_count': len(stable_messages),
            'dynamic_appendix_message_count': len(dynamic_appendix_messages),
            'turn_overlay_present': bool(turn_overlay_text),
            'turn_overlay_section_count': len(turn_overlay_parts),
            'turn_overlay_character_count': len(turn_overlay_text),
            'attachment_reopen_targets': list(attachment_reopen_targets),
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
            repair_required_tool_items=list(context_sources.get('repair_required_tool_items') or []),
            repair_required_skill_items=list(context_sources.get('repair_required_skill_items') or []),
            trace=trace,
            turn_overlay_text=turn_overlay_text,
            cache_family_revision=(
                str(context_sources['capability_snapshot'].exposure_revision or '').strip()
                or DEFAULT_CACHE_FAMILY_REVISION
            ),
        )
