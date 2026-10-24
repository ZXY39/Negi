"""中途插入消息的分类、剥离与插入网关。

"中途插入"只说明消息从哪个安全边界进入上下文；进来之后活多久由 `durability`
单独表达。所有剥离点只读这张表，新增一族只改这里并补家族测试。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from g3ku.runtime.frontdoor.tool_contract import is_frontdoor_tool_contract_message
from g3ku.runtime.skill_roster_notice import (
    is_runtime_notice_message,
    strip_skill_roster_notice_echo,
)
from g3ku.runtime.tool_contract_block import contract_pairs_from_value
from g3ku.runtime.tool_history import extract_call_id
from main.runtime.append_notice_context import (
    SYSTEM_RELAY_NOTICE_PREFIXES,
    append_notice_tail_prefix_match,
)
from main.runtime.node_prompt_contract import is_node_dynamic_contract_message
from main.runtime.tool_names import SILENT_TOOL_NAME
from main.runtime.turn_overlay import is_turn_only_system_note_message

LANE_FRONTDOOR = "frontdoor"
LANE_NODE = "node"
LANES = (LANE_FRONTDOOR, LANE_NODE)

STAGE_DURABLE = "durable"
STAGE_PROMPT_TAIL = "prompt_tail"
STAGE_COMPACTION = "compaction"
STAGE_POST_COMPACTION = "post_compaction"
# 跨轮可比性 / scaffold 采纳探针使用的逐条投影：比 durable 多剥动态 overlay。
STAGE_PROJECTION = "projection"
STAGES = (
    STAGE_DURABLE,
    STAGE_PROMPT_TAIL,
    STAGE_COMPACTION,
    STAGE_POST_COMPACTION,
    STAGE_PROJECTION,
)

LEVEL_MESSAGE = "message"
LEVEL_MARKER = "marker"

KIND_RUNTIME_NOTICE = "runtime_notice"
KIND_TURN_ONLY_NOTE = "turn_only_note"
KIND_APPEND_NOTICE = "append_notice"
KIND_RETRIEVAL_MEMORY_SNAPSHOT = "retrieval_memory_snapshot"
KIND_RETRIEVAL_OVERLAY = "retrieval_overlay"
KIND_HIDDEN_INTERNAL_EVENT = "hidden_internal_event"
KIND_LEGACY_RUNTIME_CONTRACT = "legacy_runtime_contract"
KIND_TOOL_CONTRACT_BLOCK = "tool_contract_block"
KIND_SILENT_TRACE = "silent_trace"
KIND_SYSTEM_RELAY_NOTICE = "system_relay_notice"

POSITION_TAIL = "tail"
POSITION_TAIL_BEFORE_TURN_OVERLAY = "tail_before_turn_overlay"
POSITION_BEFORE_CURRENT_USER = "before_current_user"
POSITION_AFTER_BOOTSTRAP_USER = "after_bootstrap_user"
POSITION_BODY_FRONT = "body_front"
POSITION_STABLE_SLOT = "stable_slot"
POSITION_SEEDED = "seeded"
POSITION_EMBEDDED = "embedded"
POSITION_RETIRED = "retired"

# 动态 overlay 块头：与 message_builder 的常量同值；这里只做分类，不生产。
MEMORY_WRITE_HINT_HEADER = "## 长期记忆写入提示"
RETRIEVED_MEMORY_HINT_HEADER = "## 已检索记忆使用提示"
MEMORY_SNAPSHOT_HEADING = "## 长期记忆\n"

# 续跑基线只存 role/content，metadata 会丢；内容前缀是第二判据。
# 与 `g3ku.runtime.web_ceo_sessions._internal_prompt_kind` 及 cron 生产端保持一致。
_HIDDEN_INTERNAL_CONTENT_PREFIXES = (
    "This is a background heartbeat",
    "# Heartbeat Rules",
    "[SESSION EVENTS]",
    "[CRON INTERNAL EVENT]",
    "你接收到了之前你定时的任务，如下：",
)


@dataclass(frozen=True)
class MidInsertFamily:
    kind: str
    level: str
    priority: int
    identity: Callable[[dict[str, Any]], str]
    position: str
    durability: str
    compaction: str
    classify: Callable[[dict[str, Any]], bool] | None = None
    # (lane, stage) 组合：这些剥离点会丢弃本族。空集表示任何 generic strip 都不许碰它。
    strip_at: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    # 同一请求最多一份（每跳重建的家族）；False 表示按 identity 去重、允许并存。
    singleton: bool = False
    echo_guard: Callable[[str], str] | None = None


@dataclass(frozen=True)
class RuntimeMarker:
    kind: str
    identity: str
    tool_id: str = ""
    fingerprint: str = ""
    call_ids: tuple[str, ...] = ()


def _message_role(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("role") or "").strip().lower()


def _content_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    return str(message.get("content") or "")


def _content_identity(message: dict[str, Any]) -> str:
    return hashlib.sha1(_content_text(message).strip().encode("utf-8")).hexdigest()


def _append_notice_identity(message: dict[str, Any]) -> str:
    prefix = append_notice_tail_prefix_match(message)
    content = _content_text(message).strip()
    if not prefix:
        return _content_identity(message)
    return "notice_tail:" + content[len(prefix) :]


def _is_memory_snapshot(message: dict[str, Any]) -> bool:
    if _message_role(message) != "assistant":
        return False
    return _content_text(message).strip().startswith(MEMORY_SNAPSHOT_HEADING)


def _is_dynamic_overlay(message: dict[str, Any]) -> bool:
    content = _content_text(message).strip()
    return content.startswith(MEMORY_WRITE_HINT_HEADER) or content.startswith(RETRIEVED_MEMORY_HINT_HEADER)


def _is_hidden_internal_event(message: dict[str, Any]) -> bool:
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    if str(metadata.get("internal_prompt_kind") or "").strip():
        return True
    if str(metadata.get("source") or "").strip().lower() in {"heartbeat", "cron"} and metadata.get("ui_visible") is False:
        return True
    content = _content_text(message).lstrip()
    return content.startswith(_HIDDEN_INTERNAL_CONTENT_PREFIXES)


def _is_system_relay_notice_message(message: dict[str, Any]) -> bool:
    if _message_role(message) != "user":
        return False
    return _content_text(message).strip().startswith(SYSTEM_RELAY_NOTICE_PREFIXES)


def _is_legacy_runtime_contract(message: dict[str, Any]) -> bool:
    # 前门与节点两族的形体相同；剥离语义一致，合并为一族，避免一次命中两族。
    return is_frontdoor_tool_contract_message(message) or is_node_dynamic_contract_message(message)


def silent_trace_call_ids(message: dict[str, Any]) -> tuple[str, ...]:
    """assistant 行里 silent 调用的 call id（按出现顺序）。"""
    if _message_role(message) != "assistant":
        return ()
    call_ids: list[str] = []
    for call in list(message.get("tool_calls") or []):
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or call.get("name") or "").strip()
        if name != SILENT_TOOL_NAME:
            continue
        call_id = extract_call_id(call.get("id"))
        if call_id and call_id not in call_ids:
            call_ids.append(call_id)
    return tuple(call_ids)


MID_INSERT_FAMILIES: tuple[MidInsertFamily, ...] = (
    MidInsertFamily(
        kind=KIND_HIDDEN_INTERNAL_EVENT,
        level=LEVEL_MESSAGE,
        priority=10,
        classify=_is_hidden_internal_event,
        identity=_content_identity,
        position=POSITION_SEEDED,
        durability="persistent",
        compaction="keep",
    ),
    MidInsertFamily(
        kind=KIND_APPEND_NOTICE,
        level=LEVEL_MESSAGE,
        priority=20,
        classify=lambda message: bool(append_notice_tail_prefix_match(message)),
        identity=_append_notice_identity,
        position=POSITION_AFTER_BOOTSTRAP_USER,
        durability="compression_immune",
        compaction="carry",
    ),
    MidInsertFamily(
        kind=KIND_RUNTIME_NOTICE,
        level=LEVEL_MESSAGE,
        priority=30,
        classify=is_runtime_notice_message,
        identity=_content_identity,
        position=POSITION_TAIL_BEFORE_TURN_OVERLAY,
        durability="durable_until_compaction",
        compaction="drop",
        strip_at=frozenset(
            {
                (LANE_FRONTDOOR, STAGE_POST_COMPACTION),
                (LANE_NODE, STAGE_POST_COMPACTION),
            }
        ),
        echo_guard=strip_skill_roster_notice_echo,
    ),
    MidInsertFamily(
        kind=KIND_TURN_ONLY_NOTE,
        level=LEVEL_MESSAGE,
        priority=40,
        classify=is_turn_only_system_note_message,
        identity=_content_identity,
        position=POSITION_TAIL,
        durability="turn_only",
        compaction="drop",
        strip_at=frozenset(
            {
                (LANE_FRONTDOOR, STAGE_DURABLE),
                (LANE_FRONTDOOR, STAGE_PROMPT_TAIL),
                (LANE_FRONTDOOR, STAGE_PROJECTION),
                (LANE_NODE, STAGE_PROMPT_TAIL),
            }
        ),
        singleton=True,
    ),
    MidInsertFamily(
        kind=KIND_LEGACY_RUNTIME_CONTRACT,
        level=LEVEL_MESSAGE,
        priority=50,
        classify=_is_legacy_runtime_contract,
        identity=_content_identity,
        position=POSITION_RETIRED,
        durability="retired",
        compaction="drop",
        strip_at=frozenset(
            {
                (LANE_FRONTDOOR, STAGE_DURABLE),
                (LANE_FRONTDOOR, STAGE_PROMPT_TAIL),
                (LANE_FRONTDOOR, STAGE_COMPACTION),
                (LANE_FRONTDOOR, STAGE_PROJECTION),
                (LANE_NODE, STAGE_PROMPT_TAIL),
            }
        ),
    ),
    MidInsertFamily(
        kind=KIND_SYSTEM_RELAY_NOTICE,
        level=LEVEL_MESSAGE,
        priority=60,
        classify=_is_system_relay_notice_message,
        identity=_content_identity,
        position=POSITION_TAIL,
        durability="persistent",
        compaction="keep",
    ),
    MidInsertFamily(
        kind=KIND_RETRIEVAL_MEMORY_SNAPSHOT,
        level=LEVEL_MESSAGE,
        priority=70,
        classify=_is_memory_snapshot,
        identity=_content_identity,
        position=POSITION_STABLE_SLOT,
        durability="rebuild",
        compaction="rebuild",
        strip_at=frozenset(
            {
                (LANE_FRONTDOOR, STAGE_DURABLE),
                (LANE_FRONTDOOR, STAGE_COMPACTION),
                (LANE_FRONTDOOR, STAGE_PROJECTION),
            }
        ),
    ),
    MidInsertFamily(
        kind=KIND_RETRIEVAL_OVERLAY,
        level=LEVEL_MESSAGE,
        priority=80,
        classify=_is_dynamic_overlay,
        identity=_content_identity,
        position=POSITION_TAIL,
        durability="rebuild",
        compaction="rebuild",
        strip_at=frozenset({(LANE_FRONTDOOR, STAGE_PROJECTION)}),
    ),
    MidInsertFamily(
        kind=KIND_TOOL_CONTRACT_BLOCK,
        level=LEVEL_MARKER,
        priority=90,
        identity=lambda message: "",
        position=POSITION_EMBEDDED,
        durability="until_compaction",
        compaction="keep",
    ),
    MidInsertFamily(
        kind=KIND_SILENT_TRACE,
        level=LEVEL_MARKER,
        priority=100,
        identity=lambda message: ",".join(silent_trace_call_ids(message)),
        position=POSITION_EMBEDDED,
        durability="until_subject_task_terminal",
        compaction="conditional",
    ),
)

_FAMILY_BY_KIND = {family.kind: family for family in MID_INSERT_FAMILIES}
_MESSAGE_FAMILIES = tuple(family for family in MID_INSERT_FAMILIES if family.level == LEVEL_MESSAGE)

_STAGE_DEFAULTS: dict[tuple[str, str], frozenset[str]] = {
    (LANE_FRONTDOOR, STAGE_DURABLE): frozenset(
        {
            KIND_LEGACY_RUNTIME_CONTRACT,
            KIND_TURN_ONLY_NOTE,
            KIND_RETRIEVAL_MEMORY_SNAPSHOT,
        }
    ),
    (LANE_FRONTDOOR, STAGE_PROMPT_TAIL): frozenset(
        {KIND_LEGACY_RUNTIME_CONTRACT, KIND_TURN_ONLY_NOTE}
    ),
    (LANE_FRONTDOOR, STAGE_COMPACTION): frozenset(
        {KIND_LEGACY_RUNTIME_CONTRACT, KIND_RETRIEVAL_MEMORY_SNAPSHOT}
    ),
    (LANE_FRONTDOOR, STAGE_POST_COMPACTION): frozenset({KIND_RUNTIME_NOTICE}),
    (LANE_FRONTDOOR, STAGE_PROJECTION): frozenset(
        {
            KIND_LEGACY_RUNTIME_CONTRACT,
            KIND_TURN_ONLY_NOTE,
            KIND_RETRIEVAL_MEMORY_SNAPSHOT,
            KIND_RETRIEVAL_OVERLAY,
        }
    ),
    (LANE_NODE, STAGE_PROMPT_TAIL): frozenset(
        {KIND_LEGACY_RUNTIME_CONTRACT, KIND_TURN_ONLY_NOTE}
    ),
    (LANE_NODE, STAGE_POST_COMPACTION): frozenset({KIND_RUNTIME_NOTICE}),
}


def family_for(kind: str) -> MidInsertFamily | None:
    return _FAMILY_BY_KIND.get(str(kind or "").strip())


def classify_message(message: Any) -> str | None:
    """整条消息所属的家族；metadata 优先，其次前缀/抬头。首个命中者获胜。"""
    if not isinstance(message, dict):
        return None
    for family in _MESSAGE_FAMILIES:
        classifier = family.classify
        if classifier is not None and classifier(dict(message)):
            return family.kind
    return None


def scan_markers(message: Any) -> tuple[RuntimeMarker, ...]:
    """扫描消息正文里的运行时 marker；marker 不改变消息归属。"""
    if not isinstance(message, dict):
        return ()
    markers: list[RuntimeMarker] = []
    role = _message_role(message)
    if role == "tool":
        for tool_id, fingerprint in contract_pairs_from_value(message.get("content")):
            markers.append(
                RuntimeMarker(
                    kind=KIND_TOOL_CONTRACT_BLOCK,
                    identity=f"{tool_id}:{fingerprint}",
                    tool_id=tool_id,
                    fingerprint=fingerprint,
                )
            )
    if role == "assistant":
        call_ids = silent_trace_call_ids(message)
        if call_ids:
            markers.append(
                RuntimeMarker(
                    kind=KIND_SILENT_TRACE,
                    identity=",".join(call_ids),
                    call_ids=call_ids,
                )
            )
    return tuple(markers)


def _allowed_families(
    *,
    stage: str,
    lane: str,
    families: Iterable[str] | None,
) -> frozenset[str]:
    if families is not None:
        return frozenset(str(item or "").strip() for item in families if str(item or "").strip())
    return _STAGE_DEFAULTS.get((lane, stage), frozenset())


def keeps_record_for_stage(
    record: Any,
    *,
    stage: str,
    lane: str,
    families: Iterable[str] | None = None,
) -> bool:
    if not isinstance(record, dict):
        return False
    kind = classify_message(record)
    if kind is None:
        return True
    return kind not in _allowed_families(stage=stage, lane=lane, families=families)


def strip_for_stage(
    messages: Iterable[Any] | None,
    *,
    stage: str,
    lane: str,
    families: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """按 (lane, stage) 的默认集合剥离；`families` 覆盖默认集合，供单族子站点使用。"""
    allowed = _allowed_families(stage=stage, lane=lane, families=families)
    if not allowed:
        return [dict(item) for item in list(messages or []) if isinstance(item, dict)]
    kept: list[dict[str, Any]] = []
    for item in list(messages or []):
        if not isinstance(item, dict):
            continue
        kind = classify_message(item)
        if kind is not None and kind in allowed:
            continue
        kept.append(dict(item))
    return kept


def compaction_policy(kind: str) -> str:
    family = family_for(kind)
    return family.compaction if family is not None else "keep"


def strip_after_compaction(
    messages: Iterable[Any] | None,
    *,
    lane: str,
) -> list[dict[str, Any]]:
    """压缩跳的统一下车钩子：所有 `compaction=drop` 的家族在这里离场。"""
    return strip_for_stage(messages, stage=STAGE_POST_COMPACTION, lane=lane)


def split_runtime_contract_messages(
    messages: Iterable[Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """拆出旧合同消息与其余正文。"""
    body: list[dict[str, Any]] = []
    contracts: list[dict[str, Any]] = []
    for item in list(messages or []):
        if not isinstance(item, dict):
            continue
        if classify_message(item) == KIND_LEGACY_RUNTIME_CONTRACT:
            contracts.append(dict(item))
        else:
            body.append(dict(item))
    return body, contracts


def _insertion_index(messages: list[dict[str, Any]], position: str) -> int:
    if position == POSITION_TAIL:
        return len(messages)
    system_end = 0
    while system_end < len(messages) and _message_role(messages[system_end]) == "system":
        system_end += 1
    if position == POSITION_BODY_FRONT:
        return system_end
    if position == POSITION_STABLE_SLOT:
        return 1 if system_end else 0
    if position == POSITION_AFTER_BOOTSTRAP_USER:
        # 旧帧可能把通知前插在 system 之前；跳过所有 mid-insert 家族后找真正的
        # bootstrap user，保证新块仍落在它之后而不是叠在 system 前。
        for index, item in enumerate(messages):
            if _message_role(item) == "user" and classify_message(item) is None:
                return index + 1
        index = 0
        while index < len(messages) and (
            _message_role(messages[index]) == "system"
            or classify_message(messages[index]) is not None
        ):
            index += 1
        return index
    if position == POSITION_BEFORE_CURRENT_USER:
        for index in range(len(messages) - 1, -1, -1):
            if _message_role(messages[index]) != "user":
                continue
            if classify_message(messages[index]) is not None:
                continue
            return index
        return len(messages)
    raise ValueError(f"unknown mid-insert position: {position}")


def _assert_not_inside_tool_group(messages: list[dict[str, Any]], index: int) -> None:
    if index > 0:
        previous = messages[index - 1]
        if _message_role(previous) == "assistant" and list(previous.get("tool_calls") or []):
            raise ValueError("refuse to insert mid messages between a tool call and its results")
    if index < len(messages) and _message_role(messages[index]) == "tool":
        raise ValueError("refuse to insert mid messages between a tool call and its results")


def insert_mid_messages(
    messages: Iterable[Any] | None,
    *,
    items: Iterable[Any],
    position: str,
    lane: str,
) -> list[dict[str, Any]]:
    """去重、固定顺序、位置安全地插入。

    同一 identity 的副本丢弃；`singleton` 家族先清掉旧副本（turn-only 每跳重建）。
    """
    _ = lane
    records = [dict(item) for item in list(messages or []) if isinstance(item, dict)]
    candidates = [
        dict(item)
        for item in list(items or [])
        if isinstance(item, dict) and classify_message(item) is not None
    ]
    if not candidates:
        return records

    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in sorted(
        candidates,
        key=lambda record: (
            family_for(classify_message(record) or "") or MID_INSERT_FAMILIES[-1]
        ).priority,
    ):
        by_kind.setdefault(str(classify_message(item)), []).append(item)

    kept_items: list[dict[str, Any]] = []
    for kind, family_items in by_kind.items():
        family = family_for(kind)
        if family is None:
            continue
        if family.singleton:
            records = [item for item in records if classify_message(item) != kind]
            existing_identities: set[str] = set()
        else:
            existing_identities = {
                family.identity(item)
                for item in records
                if classify_message(item) == kind
            }
        for item in family_items:
            identity = family.identity(item)
            if identity in existing_identities:
                continue
            existing_identities.add(identity)
            kept_items.append(item)
    if not kept_items:
        return records
    index = _insertion_index(records, position)
    _assert_not_inside_tool_group(records, index)
    return [*records[:index], *kept_items, *records[index:]]


def strip_turn_only_notes(messages: Iterable[Any] | None, *, lane: str) -> list[dict[str, Any]]:
    return strip_for_stage(
        messages,
        stage=STAGE_PROMPT_TAIL,
        lane=lane,
        families={KIND_TURN_ONLY_NOTE},
    )


__all__ = [
    "KIND_APPEND_NOTICE",
    "KIND_HIDDEN_INTERNAL_EVENT",
    "KIND_LEGACY_RUNTIME_CONTRACT",
    "KIND_RETRIEVAL_MEMORY_SNAPSHOT",
    "KIND_RETRIEVAL_OVERLAY",
    "KIND_RUNTIME_NOTICE",
    "KIND_SILENT_TRACE",
    "KIND_SYSTEM_RELAY_NOTICE",
    "KIND_TOOL_CONTRACT_BLOCK",
    "KIND_TURN_ONLY_NOTE",
    "LANE_FRONTDOOR",
    "LANE_NODE",
    "LANES",
    "LEVEL_MARKER",
    "LEVEL_MESSAGE",
    "MID_INSERT_FAMILIES",
    "MidInsertFamily",
    "POSITION_AFTER_BOOTSTRAP_USER",
    "POSITION_BEFORE_CURRENT_USER",
    "POSITION_BODY_FRONT",
    "POSITION_EMBEDDED",
    "POSITION_RETIRED",
    "POSITION_SEEDED",
    "POSITION_STABLE_SLOT",
    "POSITION_TAIL",
    "POSITION_TAIL_BEFORE_TURN_OVERLAY",
    "RuntimeMarker",
    "STAGE_COMPACTION",
    "STAGE_DURABLE",
    "STAGE_POST_COMPACTION",
    "STAGE_PROJECTION",
    "STAGE_PROMPT_TAIL",
    "STAGES",
    "classify_message",
    "compaction_policy",
    "family_for",
    "insert_mid_messages",
    "keeps_record_for_stage",
    "scan_markers",
    "silent_trace_call_ids",
    "split_runtime_contract_messages",
    "strip_after_compaction",
    "strip_for_stage",
    "strip_turn_only_notes",
]
