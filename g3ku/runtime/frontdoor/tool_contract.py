from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from g3ku.utils.helpers import (
    PATH_ANCHOR_RULE_TEXT,
    path_anchor_tokens,
    render_candidate_tool_line,
)

FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND = 'frontdoor_runtime_tool_contract'
FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING = '## Runtime Tool Contract'
FRONTDOOR_DYNAMIC_TOOL_CONTRACT_PAYLOAD_KEY = '_frontdoor_tool_contract_payload'
# 回显裁剪总开关（两个消费点：`_graph_normalize_model_output` 与渠道出站
# `sanitize_channel_outbound_text`）。裁剪按"任意位置子串匹配块前缀"实现，会把用户答案里
# 合法引用的块齐根截断，因此默认关；恢复前必须换成与当前注入真块逐字比对的实现。
# standalone 打回守卫（`is_frontdoor_tool_contract_echo_text`）不受此开关影响，始终生效。
ECHO_STRIP_ENABLED = False
RUNTIME_APPENDIX_HEADINGS = (FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING,)

# 钉住的静态声明：技能名单、执行策略、会话临时目录进的是 `stable_messages[0]` 那段
# system_prompt 文本（`message_builder` 拼 `## Capability Exposure Snapshot` 的同一位置），
# 不是新消息项——因此尾部重铺那条路（`_with_dynamic_appendix_at_tail` 只剥"运行时块"）
# 根本碰不到它，也就不需要第三套消息分类。
# 刷新键 = skill roster epoch（只在重印边界递增）。执行策略与临时目录**不进判据**：
# 缓存命中第一，它们的变化由一次性 `[RUNTIME_NOTICE]` 消息纠正，头部留到下次压缩统一刷新。
# epoch 只在重印边界（token 压缩、手动压缩）递增——可见集在两次重印之间怎么变，头部都
# 逐字节不动：新变为可读的 skill 由一次性持久提示落进历史（`g3ku/runtime/skill_roster_notice.py`），
# 被删/被收回的名字留在头部名单里直到下一次重印，模型去 load 时由
# `load_skill_context` 的三态回执告知原因。
FRONTDOOR_PINNED_CONTRACT_HEADING = '## Runtime Contract (pinned)'
FRONTDOOR_PINNED_CONTRACT_KIND = 'frontdoor_runtime_pinned_contract'
# v6: 头部新增「在跑的后台进程」清单块（按 session/节点冻结，随重印边界刷新）。
# v5: 头部新增「在挂的长期任务」清单块（随重印边界冻结，不逐跳重画）。版本进重印判据，
# 存量钉住块上线后重印一次；之后清单只在压缩边界跟着重印。
PINNED_CONTRACT_RENDERER_VERSION = 'pinned-contract-v6'
PINNED_CONTRACT_SNAPSHOT_NOTE = (
    'snapshot_note: this block refreshes only at compaction; later runtime notices '
    'supersede changed values.'
)
FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR = '_frontdoor_pinned_contract_text'
FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR = '_frontdoor_pinned_contract_revision'
FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR = '_frontdoor_pinned_contract_skill_ids'

# 进程级钉住表：会话对象属性之外的第二载体。前门一个回合内有两条装配路（回合起点的
# `message_builder`、同回合每一跳的 `prompt contract`），它们拿到的 session 实例不同；
# `state` 里的键会被归一化白名单静默丢掉，不能当载体。表按 session_key 分桶、LRU 封顶，
# 只在进程内活着——重启后首跳重钉一次，代价是那一次整段重算。
_PINNED_CONTRACT_STORE_LIMIT = 128
_PINNED_CONTRACT_STORES: 'OrderedDict[str, dict[str, Any]]' = OrderedDict()

# skill roster epoch：头部名单的重印计数。与 `tools[]` 的重印边界同源
# （`_frontdoor_bundle_recommit_boundary` 返回 True 时 +1），两份名单共用一套
# "什么时候允许变"的口径。双载体与钉住表同款。
_SKILL_ROSTER_EPOCH_ATTR = '_frontdoor_skill_roster_epoch'
_SKILL_ROSTER_EPOCH_STORE_LIMIT = 256
_SKILL_ROSTER_EPOCH_STORES: 'OrderedDict[str, int]' = OrderedDict()


def skill_roster_epoch_for(session: Any, *, session_key: str | None = None) -> int:
    """当前 skill 名单的重印代数；没有记录时从 0 起。"""
    raw = getattr(session, _SKILL_ROSTER_EPOCH_ATTR, None) if session is not None else None
    try:
        session_value = int(raw or 0)
    except (TypeError, ValueError):
        session_value = 0
    key = str(session_key or '').strip()
    if not key:
        return session_value
    stored = _SKILL_ROSTER_EPOCH_STORES.get(key)
    if stored is None:
        return session_value
    return max(session_value, int(stored))


def bump_skill_roster_epoch(session: Any, *, session_key: str | None = None) -> int:
    """重印边界处 +1；返回新值。写失败不致命（进程表兜底）。"""
    next_value = skill_roster_epoch_for(session, session_key=session_key) + 1
    if session is not None:
        try:
            setattr(session, _SKILL_ROSTER_EPOCH_ATTR, next_value)
        except Exception:
            pass
    key = str(session_key or '').strip()
    if key:
        _SKILL_ROSTER_EPOCH_STORES[key] = next_value
        _SKILL_ROSTER_EPOCH_STORES.move_to_end(key)
        while len(_SKILL_ROSTER_EPOCH_STORES) > _SKILL_ROSTER_EPOCH_STORE_LIMIT:
            _SKILL_ROSTER_EPOCH_STORES.popitem(last=False)
    return next_value


def clear_pinned_contract_stores() -> None:
    """测试隔离：钉住表是进程级的，跨用例可见。"""
    _PINNED_CONTRACT_STORES.clear()
    _SKILL_ROSTER_EPOCH_STORES.clear()
    from g3ku.runtime.frontdoor.capability_snapshot import (
        clear_pinned_exposure_revision_stores,
    )

    clear_pinned_exposure_revision_stores()


def pinned_head_carrier(*, session_key: str) -> dict[str, Any]:
    """把当前钉住的头部导成可持久化的载荷；没钉住就返回空表。

    节点道没有会话侧车那样的载体，耐久那份是节点自己的 selection trace（整份已持久化）。
    导出内容与 `restore_pinned_contract_state` 的入参同形，读写两侧不允许各自拼字段。
    """
    text, revision, skill_ids = _pinned_contract_entry_from_store(session_key=str(session_key or "").strip())
    if not text or not revision:
        return {}
    return {
        "text": text,
        "revision": revision,
        "skill_ids": list(skill_ids),
        "epoch": skill_roster_epoch_for(None, session_key=str(session_key or "").strip()),
    }


def _pinned_contract_entry_from_store(*, session_key: str) -> tuple[str, str, list[Any]]:
    """进程表里那一份（会话对象之外的第二载体）；节点道只用这一份。"""
    key = str(session_key or "").strip()
    stored = _PINNED_CONTRACT_STORES.get(key) if key else None
    if not isinstance(stored, dict):
        return "", "", []
    text = stored.get(FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR)
    return (
        text if isinstance(text, str) else "",
        str(stored.get(FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR) or "").strip(),
        list(stored.get(FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR) or []),
    )


def restore_pinned_contract_state(
    session: Any,
    *,
    session_key: str,
    text: Any,
    revision: Any,
    skill_ids: list[Any] | None,
    epoch: Any,
) -> None:
    """把钉住的头部原文与重印代数放回两份载体（重启/续跑恢复用）。

    只写会话对象挡不住第二个装配点：`skill_roster_epoch_for` 取 max(会话值, 表值)、
    `_pinned_entries` 也读表。epoch 必须按持久值设置而不是从头计——写回 0 会让判据键与
    存下的原文对不上，恢复就等于没恢复。原文不 strip：那串字节是上一跳真发出去的内容。
    """
    pinned_text = str(text or "")
    normalized_revision = str(revision or "").strip()
    normalized_skill_ids = _normalized_name_list(skill_ids)
    try:
        normalized_epoch = max(0, int(epoch or 0))
    except (TypeError, ValueError):
        normalized_epoch = 0
    if session is not None:
        try:
            setattr(session, FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR, pinned_text)
            setattr(session, FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR, normalized_revision)
            setattr(session, FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR, list(normalized_skill_ids))
            setattr(session, _SKILL_ROSTER_EPOCH_ATTR, normalized_epoch)
        except Exception:
            pass
    if pinned_text and normalized_revision:
        _write_pinned_contract_entry(
            None,
            session_key=str(session_key or "").strip(),
            revision=normalized_revision,
            text=pinned_text,
            skill_ids=list(normalized_skill_ids),
        )
    key = str(session_key or "").strip()
    if key:
        _SKILL_ROSTER_EPOCH_STORES[key] = normalized_epoch
        _SKILL_ROSTER_EPOCH_STORES.move_to_end(key)
        while len(_SKILL_ROSTER_EPOCH_STORES) > _SKILL_ROSTER_EPOCH_STORE_LIMIT:
            _SKILL_ROSTER_EPOCH_STORES.popitem(last=False)


def drop_pinned_contract_state(session: Any, *, session_key: str | None = None) -> None:
    """撤掉这一条会话的钉住头部与 roster epoch，两份载体一起撤。

    截断到会话开头要用它：空基线之后头部必须按当轮全量名单重钉。只清会话对象那一份
    挡不住——`skill_roster_epoch_for` 取 max(会话值, 表值)，`_pinned_entries` 也读表，
    表里残留的那一份会让下一跳继续复用截断前的头部字节。
    """
    if session is not None:
        try:
            setattr(session, FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR, '')
            setattr(session, FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR, '')
            setattr(session, FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR, [])
            setattr(session, _SKILL_ROSTER_EPOCH_ATTR, 0)
        except Exception:
            pass
    key = str(session_key or '').strip()
    if not key:
        return
    _PINNED_CONTRACT_STORES.pop(key, None)
    _SKILL_ROSTER_EPOCH_STORES.pop(key, None)
    from g3ku.runtime.frontdoor.capability_snapshot import drop_pinned_exposure_revision

    drop_pinned_exposure_revision(key)


def pinned_contract_revision_key(
    *,
    exec_runtime_policy: dict[str, Any] | None,
    session_temp_dir: str | None,
    skill_roster_epoch: int,
    sandbox_tier: str = "",
    tool_policy_signature: str = "",
) -> str:
    """头部块的重印判据：roster epoch + 沙箱档位。

    执行策略、临时目录、受限档工具白名单**刻意不进判据**：缓存命中第一。它们变化时
    不重印头部，改由一次性 `[RUNTIME_NOTICE]` 消息纠正；下次压缩 epoch 跳变时，头部
    才用当刻值重印。可见集本身同样不进键：两次重印之间可见集怎么变，头部都逐字节
    不动——新增的可读 skill 由一次性持久提示落进历史，被删/被收回的名字留在头部直到
    下一次重印（`load_skill_context` 三态回执告知原因），删除/收回另有一条尾部通知。

    唯一的显式例外是**沙箱档位切换**：切档会同时换掉模型可见的 `tools[]` 与头部声明，
    继续沿用旧前缀会让模型按旧工具面行动；这是一次操作员显式动作，接受这一次 cache miss。
    同一档位内改受限工具白名单不算切档，走 `denied_tools` / `candidate_tools` 通知。
    """
    _ = (exec_runtime_policy, session_temp_dir, tool_policy_signature)
    digest = hashlib.sha256(
        '\n'.join(
            (
                PINNED_CONTRACT_RENDERER_VERSION,
                str(int(skill_roster_epoch or 0)),
                str(sandbox_tier or "").strip(),
            )
        ).encode('utf-8')
    ).hexdigest()[:16]
    return f'pc:{digest}'


def _pinned_contract_legacy_revision_key(
    *,
    exec_runtime_policy: dict[str, Any] | None,
    session_temp_dir: str | None,
    skill_roster_epoch: int,
    sandbox_tier: str = "",
    tool_policy_signature: str = "",
) -> str:
    """上线迁移用：旧公式把 tool_policy_signature 算进 digest。

    存量会话的侧车/进程表里存的是旧键；新键查不到时用旧键回查一次，命中就把同一串
    原文收养到新键下，避免上线首跳整段重印头部。
    """
    _ = (exec_runtime_policy, session_temp_dir)
    digest = hashlib.sha256(
        '\n'.join(
            (
                PINNED_CONTRACT_RENDERER_VERSION,
                str(int(skill_roster_epoch or 0)),
                str(sandbox_tier or "").strip(),
                str(tool_policy_signature or "").strip(),
            )
        ).encode('utf-8')
    ).hexdigest()[:16]
    return f'pc:{digest}'


def _normalize_skill_roster_items(items: list[Any] | None) -> list[dict[str, str]]:
    """把名单入参归一成 `{skill_id, display_name, description}`，按 id 排序去重。

    语义筛选删除后，"选哪个 skill"的判断在模型侧，描述是必要输入：名单渲染必须
    带名称与一句话描述，纯 id 行不够用。
    """
    entries: dict[str, dict[str, str]] = {}
    for item in list(items or []):
        if isinstance(item, dict):
            skill_id = str(item.get('skill_id') or '').strip()
            display_name = str(item.get('display_name') or item.get('name') or '').strip()
            description = str(item.get('description') or '').strip()
        else:
            skill_id = str(getattr(item, 'skill_id', '') or '').strip()
            display_name = str(getattr(item, 'display_name', '') or getattr(item, 'name', '') or '').strip()
            description = str(getattr(item, 'description', '') or '').strip()
        if not skill_id or skill_id in entries:
            continue
        entries[skill_id] = {
            'skill_id': skill_id,
            'display_name': display_name or skill_id,
            'description': description,
        }
    return [entries[key] for key in sorted(entries)]


_TIER_LABELS = {
    "limited": "受限模式",
    "jailed": "沙盒模式",
    "readonly": "只读模式",
    "host_full": "全权限模式",
}

LONG_TERM_TASK_ROSTER_HEADING = '### 在挂的长期任务'
LONG_TERM_TASK_ROSTER_MAX_LINES = 8
LONG_TERM_TASK_ROSTER_TITLE_LIMIT = 60


def _long_term_task_goal(title: Any, task_id: str) -> str:
    """清单里的「目标」：任务标题压成一行、截到 60 字，`|` 换成全角保持列形。

    标题口径与 `task_list` 的「目标」一致（`TaskRecord.title`，创建时取
    `task` 前 60 字）；`core_requirement` 是每轮内容，不能当长期用途。
    """
    text = ' '.join(str(title or '').split())
    if not text or text == task_id:
        return ''
    text = text.replace('|', '｜')
    if len(text) <= LONG_TERM_TASK_ROSTER_TITLE_LIMIT:
        return text
    return text[: LONG_TERM_TASK_ROSTER_TITLE_LIMIT - 1] + '…'


def render_long_term_task_roster(items: list[Any] | None) -> str:
    """渲染「在挂的长期任务」小节；空清单返回空串（整节不出现）。

    条目由服务侧窄读给出：本会话 `status=in_progress` 且根节点 continuous 的任务。
    状态只有两档：在挂 / 暂停。行数有界，余量指向 `task_list(long_term=true)`。
    """
    entries: list[tuple[str, str, str]] = []
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        task_id = str(item.get('task_id') or '').strip()
        if not task_id:
            continue
        paused = (
            bool(item.get('paused'))
            or bool(item.get('is_paused'))
            or bool(item.get('pause_requested'))
        )
        entries.append(
            (
                task_id,
                _long_term_task_goal(item.get('title'), task_id),
                '暂停' if paused else '在挂',
            )
        )
    if not entries:
        return ''
    lines = [LONG_TERM_TASK_ROSTER_HEADING]
    visible = entries[:LONG_TERM_TASK_ROSTER_MAX_LINES]
    for task_id, goal, state in visible:
        columns = [task_id]
        if goal:
            columns.append(goal)
        columns.append(state)
        lines.append('- ' + ' | '.join(columns))
    overflow = len(entries) - len(visible)
    if overflow > 0:
        lines.append(f'另有 {overflow} 条，用 task_list(long_term=true) 查看')
    return '\n'.join(lines)


OWNED_PROCESS_ROSTER_HEADING = '### 在跑的后台进程'
OWNED_PROCESS_ROSTER_MAX_LINES = 8
OWNED_PROCESS_PURPOSE_LIMIT = 80
OWNED_PROCESS_CMD_LIMIT = 200


def _collapse_owned_process_text(value: Any) -> str:
    return ' '.join(str(value or '').split()).replace('|', '｜')


def _owned_process_purpose(item: dict[str, Any]) -> str:
    text = _collapse_owned_process_text(item.get('purpose') or item.get('label'))
    if len(text) <= OWNED_PROCESS_PURPOSE_LIMIT:
        return text
    return text[: OWNED_PROCESS_PURPOSE_LIMIT - 1] + '…'


def _owned_process_command(item: dict[str, Any]) -> str:
    text = _collapse_owned_process_text(item.get('cmd'))
    if len(text) <= OWNED_PROCESS_CMD_LIMIT:
        return text
    head = OWNED_PROCESS_CMD_LIMIT // 2 - 1
    tail = OWNED_PROCESS_CMD_LIMIT - head - 1
    return text[:head] + '…' + text[-tail:]


def _owned_process_identity_text(item: dict[str, Any]) -> str:
    purpose = _owned_process_purpose(item)
    if purpose:
        return purpose
    return _owned_process_command(item)


def render_owned_process_roster(items: list[Any] | None) -> str:
    """渲染「在跑的后台进程」小节；空清单返回空串（整节不出现）。

    条目由 `frontdoor_owned_process_roster` 给出：只含活条目，节点道已按
    `node_id` 过滤。每行 `pid | 用途 | 启动于 | 日志`，用途为空时回退到
    压缩后的命令；行数有界，按启动时间倒序。
    """
    entries: list[tuple[str, int, str, str]] = []
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        try:
            pid = int(item.get('pid') or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        entries.append(
            (
                str(item.get('started_at') or '').strip(),
                pid,
                _owned_process_identity_text(item),
                _collapse_owned_process_text(item.get('log_path')),
            )
        )
    if not entries:
        return ''
    entries.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    lines = [OWNED_PROCESS_ROSTER_HEADING]
    visible = entries[:OWNED_PROCESS_ROSTER_MAX_LINES]
    for started_at, pid, label, log_path in visible:
        columns = [str(pid)]
        if label:
            columns.append(label)
        if started_at:
            columns.append(f'启动于 {started_at}')
        if log_path:
            columns.append(f'日志 {log_path}')
        lines.append('- ' + ' | '.join(columns))
    overflow = len(entries) - len(visible)
    if overflow > 0:
        lines.append(f'另有 {overflow} 条更早的未列出')
    return '\n'.join(lines)


def session_mode_state_text(sandbox_tier: Any) -> str:
    """当刻档位叫什么名字——头部快照与合同分节用这一档。

    分节值会与头部原文逐字比对（`pinned_contract_section_values` → 台账初值），所以
    这一份不许带事件词：一条静态快照写成"已切换到 X"就是在宣布一次没发生过的切档
    （实盘：新会话首跳的通知里出现 `session_mode: 已切换到全权限模式`）。
    """
    tier = str(sandbox_tier or "").strip()
    return _TIER_LABELS.get(tier, "")


def session_mode_switch_text(sandbox_tier: Any) -> str:
    """真的换了档才用这一档（`### Session mode` 的一次性通知）。"""
    label = session_mode_state_text(sandbox_tier)
    return f"已切换到{label}" if label else ""


def _session_mode_line(sandbox_tier: Any) -> str:
    mode_text = session_mode_state_text(sandbox_tier)
    return f'session_mode: {mode_text}' if mode_text else ""


def frontdoor_long_term_task_roster(loop: Any, *, session_key: str | None) -> list[dict[str, Any]]:
    """本会话在挂长期任务的取数口（主运行时窄读的薄包装）。

    取数失败一律返回空表：清单是观测块，读不出来就整节不渲染，不阻断会话。
    """
    key = str(session_key or '').strip()
    if not key:
        return []
    service = getattr(loop, 'main_task_service', None)
    getter = getattr(service, 'long_term_task_roster', None)
    if not callable(getter):
        return []
    try:
        items = getter(key)
    except Exception:
        return []
    return [dict(item) for item in list(items or []) if isinstance(item, dict)]


def _long_term_tasks_from_provider(provider: Any) -> list[Any]:
    if not callable(provider):
        return []
    try:
        return list(provider() or [])
    except Exception:
        return []


def frontdoor_owned_process_roster(
    *,
    session_key: str = '',
    task_id: str = '',
    node_id: str = '',
) -> list[dict[str, Any]]:
    """本会话/本节点在跑后台进程的取数口（归属表窄读）。

    CEO 道传 `session_key`；节点道传 `task_id` + `node_id`，只留该节点登记的条目。
    取数失败一律返回空表：清单是观测块，读不出来就整节不渲染，不阻断会话。
    """
    normalized_session = str(session_key or '').strip()
    normalized_task = str(task_id or '').strip()
    if not normalized_session and not normalized_task:
        return []
    normalized_node = str(node_id or '').strip() or None
    try:
        from g3ku.runtime import owned_processes

        scope = owned_processes.scope_key(
            session_key=normalized_session,
            task_id=normalized_task,
        )
        entries = owned_processes.list_owned(scope, node_id=normalized_node)
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    for entry in list(entries or []):
        if not isinstance(entry, dict):
            continue
        try:
            pid = int(entry.get('pid') or 0)
        except (TypeError, ValueError):
            continue
        if pid <= 0:
            continue
        items.append(
            {
                'pid': pid,
                'purpose': str(entry.get('label') or ''),
                'cmd': str(entry.get('cmd') or ''),
                'started_at': str(entry.get('started_at') or ''),
                'log_path': str(entry.get('log_path') or ''),
            }
        )
    return items


def _owned_processes_from_provider(provider: Any) -> list[Any]:
    if not callable(provider):
        return []
    try:
        return list(provider() or [])
    except Exception:
        return []


def render_pinned_contract_text(
    *,
    skill_items: list[Any] | None,
    exec_runtime_policy: dict[str, Any] | None,
    session_temp_dir: str | None,
    include_extras: bool = True,
    sticker_items: list[Any] | None = None,
    sandbox_tier: str = "",
    session_workspace_dir: str | None = None,
    long_term_tasks: list[Any] | None = None,
    owned_processes: list[Any] | None = None,
) -> str:
    """渲染头部钉住块；`include_extras=True` 时即使名单为空也渲静态声明。

    `sticker_items` 是 `g3ku.stickers.catalog.scan_packs()` 的结果；它在钉住时点被
    冻结进原文，两次重印之间怎么增删表情包都不改这块（增删由 `[RUNTIME_NOTICE]`
    的 `sticker_pack:<pack>` 分节纠正）。`sticker_max_per_reply` 与它同属一节：
    钉住时冻结，改设置由同一条通知的 `sticker_max_per_reply` 分节纠正。

    头部是**快照**：只在重印边界（压缩/手动压缩）用当刻值重渲，两次重印之间逐字节不动。
    执行策略、会话临时目录、路径锚因此必须始终在头部有一份——名单为空就不钉，会让删掉
    尾块之后这些声明在整份上下文里消失。变化由一次性 `[RUNTIME_NOTICE]` 消息纠正。

    名单是**钉住时点的全量可见 skill**（语义筛选已删，"选哪个"由模型按描述判断），
    按 id 排序：钉住的那份是"能加载哪些"的集合声明，不带每回合的语义排名。两次重印
    之间名单会过期——新增的靠一次性持久提示点名，消失的靠
    `load_skill_context` 三态回执告知原因。

    `long_term_tasks` 是同一份快照里的「在挂的长期任务」清单（服务侧窄读结果），
    空表整节不渲染；它不做判重、不改工具参数，只是让模型看见本会话已挂着哪些长期任务。
    `owned_processes` 是同一份快照里的「在跑的后台进程」清单（CEO 按会话、节点按
    task+node 过滤），空表整节不渲染；用途取 `exec` 创建时写入的 `label`。

    `include_extras=False` 只渲名单本体：节点道若只需要名单可用这一档；
    会话语义下的节点头部现在也走 `include_extras=True`（exec 策略/临时目录/路径锚进头部）。
    """
    normalized_items = _normalize_skill_roster_items(skill_items)
    if not normalized_items and not include_extras:
        return ''
    lines = [
        FRONTDOOR_PINNED_CONTRACT_HEADING,
        f'kind: {FRONTDOOR_PINNED_CONTRACT_KIND}',
        PINNED_CONTRACT_SNAPSHOT_NOTE,
    ]
    if normalized_items:
        lines.append(
            'candidate_skills (loadable with `load_skill_context`; `id | name | description`):'
        )
        lines.extend(
            f'- `{item["skill_id"]}` | {item["display_name"]} | {item["description"]}'
            for item in normalized_items
        )
    from g3ku.stickers import render_catalog_lines
    from g3ku.stickers.config import max_per_reply

    # 发送预算与包清单同属表情包一节的静态声明：钉住时点冻结，改设置不重印头部，
    # 由 `[RUNTIME_NOTICE]` 的 `sticker_max_per_reply` 分节纠正。
    lines.append(f'sticker_max_per_reply: {max_per_reply()}')
    lines.extend(render_catalog_lines(sticker_items or ()))
    if not include_extras:
        return '\n'.join(lines).strip()
    mode_line = _session_mode_line(sandbox_tier)
    if mode_line:
        lines.append(mode_line)
    workspace_dir = str(session_workspace_dir or "").strip()
    if workspace_dir:
        lines.append(f'session_workspace_dir: {workspace_dir}')
    lines.append(_render_exec_runtime_policy(exec_runtime_policy))
    lines.extend(_render_session_temp_dir(session_temp_dir))
    lines.extend(_render_path_anchor())
    roster_text = render_long_term_task_roster(long_term_tasks)
    if roster_text:
        lines.append(roster_text)
    process_roster_text = render_owned_process_roster(owned_processes)
    if process_roster_text:
        lines.append(process_roster_text)
    return '\n'.join(lines).strip()


def _pinned_contract_entry(session: Any) -> tuple[str, str, list[Any]]:
    if session is None:
        return '', '', []
    text = getattr(session, FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR, None)
    revision = str(getattr(session, FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR, '') or '').strip()
    skill_ids = getattr(session, FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR, None)
    return (text if isinstance(text, str) else '', revision, list(skill_ids or []))


def _write_pinned_contract_entry(
    session: Any,
    *,
    session_key: str,
    revision: str,
    text: str,
    skill_ids: list[str],
) -> None:
    entry = {
        FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR: revision,
        FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR: text,
        FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR: list(skill_ids),
    }
    if session is not None:
        try:
            setattr(session, FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR, revision)
            setattr(session, FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR, text)
            setattr(session, FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR, list(skill_ids))
        except Exception:
            pass
    normalized_key = str(session_key or '').strip()
    if not normalized_key:
        return
    _PINNED_CONTRACT_STORES[normalized_key] = entry
    _PINNED_CONTRACT_STORES.move_to_end(normalized_key)
    while len(_PINNED_CONTRACT_STORES) > _PINNED_CONTRACT_STORE_LIMIT:
        _PINNED_CONTRACT_STORES.popitem(last=False)


def _pinned_entries(session: Any, *, session_key: str) -> list[tuple[str, str, list[Any]]]:
    """两份载体的当前内容：会话对象属性优先，进程表兜底。"""
    entries = [_pinned_contract_entry(session)]
    normalized_key = str(session_key or '').strip()
    if normalized_key:
        stored = _PINNED_CONTRACT_STORES.get(normalized_key)
        if isinstance(stored, dict):
            text = stored.get(FRONTDOOR_PINNED_CONTRACT_TEXT_ATTR)
            entries.append(
                (
                    text if isinstance(text, str) else '',
                    str(stored.get(FRONTDOOR_PINNED_CONTRACT_REVISION_ATTR) or '').strip(),
                    list(stored.get(FRONTDOOR_PINNED_CONTRACT_SKILLS_ATTR) or []),
                )
            )
    return entries


def _read_pinned_contract_entry(
    session: Any,
    *,
    session_key: str,
    revision: str,
) -> tuple[str, list[str]]:
    """两份载体里挑判据键对上的那份；都对不上才重钉。

    判据键相同就复用，是"两个装配点各自渲染、字节必须逐字相同"的唯一保证：前门回合起点走
    `message_builder`，同回合的每一跳走 `prompt contract`，两处拿到的 session 不是同一个实例。
    只认对象属性的话，第二个装配点每跳都当成"未钉住"去重印头部，而头部一改就顶掉它后面的
    全部前缀——比不钉严重。
    """
    for text, stored_revision, skill_ids in _pinned_entries(session, session_key=session_key):
        if text and stored_revision == revision:
            return text, _normalized_name_list(skill_ids)
    return '', []


def pinned_contract_state(session: Any, *, session_key: str | None = None) -> tuple[str, list[str]]:
    """已钉住的那份（原文 + 名单），不校验判据键——尾块差集算式的取数口。"""
    for text, _revision, skill_ids in _pinned_entries(
        session,
        session_key=str(session_key or '').strip(),
    ):
        if text:
            return text, _normalized_name_list(skill_ids)
    return '', []


def pinned_contract_section_values(pinned_text: Any) -> dict[str, str]:
    """从头部原文里取"快照已覆盖"的合同分节，作为一次性通知台账的初值。

    认头部真正渲染过的行：exec 策略、临时目录，以及每个表情包一行
    （`sticker_pack:<pack>`）。头部没有这些行（旧数据或 `include_extras=False`）时
    返回空表，通知层会把当刻值当首次快照投出去。
    """
    from g3ku.stickers import sticker_contract_sections_from_text

    values: dict[str, str] = {}
    for line in str(pinned_text or '').splitlines():
        stripped = line.strip()
        if stripped.startswith('exec_runtime_policy:'):
            body = stripped.split(':', 1)[1].strip()
            values['exec_runtime_policy'] = '' if body in {'', 'none'} else body
        elif stripped.startswith('session_temp_dir:'):
            values['session_temp_dir'] = stripped.split(':', 1)[1].strip()
        elif stripped.startswith('session_mode:'):
            values['session_mode'] = stripped.split(':', 1)[1].strip()
        elif stripped.startswith('session_workspace_dir:'):
            values['session_workspace_dir'] = stripped.split(':', 1)[1].strip()
        elif stripped.startswith('sticker_max_per_reply:'):
            values['sticker_max_per_reply'] = stripped.split(':', 1)[1].strip()
    values.update(sticker_contract_sections_from_text(pinned_text))
    return values


def frontdoor_pinned_contract_text(
    session: Any,
    *,
    skill_items: list[Any] | None,
    exec_runtime_policy: dict[str, Any] | None,
    session_temp_dir: str | None,
    skill_roster_epoch: int,
    session_key: str | None = None,
    include_extras: bool = True,
    sticker_items: list[Any] | None = None,
    sandbox_tier: str = "",
    session_workspace_dir: str | None = None,
    tool_policy_signature: str = "",
    long_term_tasks_provider: Any = None,
    owned_processes_provider: Any = None,
) -> str:
    """注入侧唯一取数口：命中已钉住的内容就复用原文，否则重钉一次并记下判据键。

    读一次写一次都在同一跳内完成，重复调用幂等（同一判据键返回同一串原文）。
    `include_extras=True` 时名单为空也会渲出静态声明（exec 策略/临时目录/路径锚），
    因此正常情况下不再返回空串；空串只出现在 `include_extras=False` 且名单为空。

    `long_term_tasks_provider` 只在重钉那一跳调用：清单随原文一起冻结，两次重印之间
    不取数、不重画（头部一改就顶掉身后全部前缀）。取数失败按空表处理，整节不渲染。
    `owned_processes_provider` 同档：CEO 传 `session_key`，节点传 `task_id`+`node_id`；
    进程增删本身不触发重印，启动回执与退出消息是两条 delta。

    没有载体（既拿不到会话对象又没有 session_key）时返回空串：钉不住就每回合现算，那份
    名单会随每回合的语义挑选变字节，头部每跳都改比不钉糟糕得多（头部一改就顶掉身后全部前缀）。
    删掉尾块之后这类路径必须由装配侧补一条声明消息，不能当成"不用钉"静默放过。
    """
    normalized_session_key = str(session_key or '').strip()
    if session is None and not normalized_session_key:
        return ''
    revision = pinned_contract_revision_key(
        exec_runtime_policy=exec_runtime_policy,
        session_temp_dir=session_temp_dir,
        skill_roster_epoch=skill_roster_epoch,
        sandbox_tier=sandbox_tier,
        tool_policy_signature=tool_policy_signature,
    )
    stored_text, _stored_skill_ids = _read_pinned_contract_entry(
        session,
        session_key=str(session_key or '').strip(),
        revision=revision,
    )
    if stored_text:
        return stored_text
    # 上线迁移：旧键把 tool_policy_signature 算进 digest。新键查不到时按旧公式回查一次，
    # 命中就把同一串原文收养到新键下；否则存量会话会在首跳整段重印头部。
    legacy_revision = _pinned_contract_legacy_revision_key(
        exec_runtime_policy=exec_runtime_policy,
        session_temp_dir=session_temp_dir,
        skill_roster_epoch=skill_roster_epoch,
        sandbox_tier=sandbox_tier,
        tool_policy_signature=tool_policy_signature,
    )
    if legacy_revision and legacy_revision != revision:
        legacy_text, legacy_skill_ids = _read_pinned_contract_entry(
            session,
            session_key=normalized_session_key,
            revision=legacy_revision,
        )
        if legacy_text:
            _write_pinned_contract_entry(
                session,
                session_key=normalized_session_key,
                revision=revision,
                text=legacy_text,
                skill_ids=list(legacy_skill_ids),
            )
            return legacy_text
    fresh = render_pinned_contract_text(
        skill_items=skill_items,
        exec_runtime_policy=exec_runtime_policy,
        session_temp_dir=session_temp_dir,
        include_extras=include_extras,
        sticker_items=sticker_items,
        sandbox_tier=sandbox_tier,
        session_workspace_dir=session_workspace_dir,
        long_term_tasks=_long_term_tasks_from_provider(long_term_tasks_provider),
        owned_processes=_owned_processes_from_provider(owned_processes_provider),
    )
    if not fresh:
        return ''
    _write_pinned_contract_entry(
        session,
        session_key=str(session_key or '').strip(),
        revision=revision,
        text=fresh,
        skill_ids=[item['skill_id'] for item in _normalize_skill_roster_items(skill_items)],
    )
    return fresh


def pinned_skill_ids_for(session: Any, *, session_key: str | None = None) -> list[str]:
    """钉住块里那份名单（差集算式的左操作数）。"""
    return pinned_contract_state(session, session_key=session_key)[1]


def split_pinned_contract_from_system_text(system_text: Any) -> tuple[str, str]:
    """把基础 system 文本拆成（不含钉住块的正文, 钉住块原文）。

    钉住块只追加在正文末尾，因此按标题行**首次**出现处切：一旦某份携带正文里已经叠了多份
    （实盘抓到过续跑头把同一份块叠了两遍），取第一份才能把它们一起摘掉，再拼回一份。
    """
    text = str(system_text or '')
    marker = f'\n\n{FRONTDOOR_PINNED_CONTRACT_HEADING}'
    index = text.find(marker)
    if index < 0:
        return text, ''
    return text[:index], text[index + 2:]


def merge_pinned_contract_into_system_text(system_text: Any, pinned_contract_text: Any) -> str:
    """写头部：摘掉旧钉住块再拼上本轮该钉的那一份，字节只在刷新键变化时才改。

    钉住内容为空时原样返回——此时尾块还带着全量声明，不该动头部已有的字节。
    """
    original = str(system_text or '')
    pinned_text = str(pinned_contract_text or '').strip()
    if not pinned_text:
        return original
    base, _existing = split_pinned_contract_from_system_text(original)
    return f'{base.rstrip()}\n\n{pinned_text}'


def apply_pinned_contract_to_head(records: list[dict[str, Any]] | None, pinned_contract_text: Any) -> list[dict[str, Any]]:
    """把钉住块并进头部那条 system 记录（首条非 system 或空表则原样返回）。"""
    pinned_text = str(pinned_contract_text or '').strip()
    normalized = [dict(item) for item in list(records or []) if isinstance(item, dict)]
    if not pinned_text or not normalized:
        return normalized
    if str(normalized[0].get('role') or '').strip().lower() != 'system':
        return normalized
    merged = dict(normalized[0])
    merged['content'] = merge_pinned_contract_into_system_text(merged.get('content'), pinned_text)
    return [merged, *normalized[1:]]


def pinned_contract_is_carried_by_head(
    records: list[dict[str, Any]] | None,
    pinned_contract_text: Any,
) -> bool:
    """尾块能不能省这三段，只看头部那条记录里是否真有这段原文。

    这是"省缓存不省声明"的闸门：装配路很多（前门四条、节点一条），任何一条没把块写进头部
    却照样让尾块省略，名单就在整份上下文里消失了——模型看不到候选技能，也不会报错。判据用
    观测而不是标志位：标志会谎报，头部原文不会。
    """
    pinned_text = str(pinned_contract_text or '').strip()
    if not pinned_text:
        return False
    normalized = [dict(item) for item in list(records or []) if isinstance(item, dict)]
    if not normalized or str(normalized[0].get('role') or '').strip().lower() != 'system':
        return False
    _base, carried = split_pinned_contract_from_system_text(normalized[0].get('content'))
    return carried.strip() == pinned_text



def _normalized_name_list(items: list[Any] | None) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for item in list(items or []):
        normalized = str(item or '').strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _normalized_candidate_tool_items(
    items: list[Any] | None,
    *,
    fallback_names: list[str] | None = None,
) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    raw_items = list(items or [])
    if not raw_items and fallback_names:
        raw_items = list(fallback_names)
    for item in raw_items:
        if isinstance(item, dict):
            tool_id = str(item.get('tool_id') or '').strip()
            description = str(item.get('description') or '').strip()
        else:
            tool_id = str(item or '').strip()
            description = ''
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        ordered.append({'tool_id': tool_id, 'description': description})
    return ordered


def _normalized_repair_required_tool_items(items: list[Any] | None) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        tool_id = str(item.get('tool_id') or '').strip()
        if not tool_id or tool_id in seen:
            continue
        seen.add(tool_id)
        ordered.append(
            {
                'tool_id': tool_id,
                'description': str(item.get('description') or '').strip(),
                'reason': str(item.get('reason') or '').strip(),
            }
        )
    return ordered


def _normalized_repair_required_skill_items(items: list[Any] | None) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get('skill_id') or '').strip()
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        ordered.append(
            {
                'skill_id': skill_id,
                'description': str(item.get('description') or '').strip(),
                'reason': str(item.get('reason') or '').strip(),
            }
        )
    return ordered


def normalize_frontdoor_candidate_tool_items(
    items: list[Any] | None,
    *,
    fallback_names: list[str] | None = None,
) -> list[dict[str, str]]:
    return _normalized_candidate_tool_items(items, fallback_names=fallback_names)


def _normalized_attachment_reopen_targets(items: list[Any] | None) -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        # 末道闸门：无论句柄从哪条路进来（本回合上传、历史 metadata、继承的
        # 任务状态），`kind=audio` 一律不进契约。语音的内容在正文转写里，
        # 音频本身只服务气泡回放，模型没有读它的能力。
        if str(item.get('kind') or '').strip().lower() == 'audio':
            continue
        path = str(item.get('path') or '').strip()
        ref = str(item.get('ref') or '').strip()
        if not path and not ref:
            continue
        dedupe_key = ref or path
        if not dedupe_key or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        entry = {
            'name': str(item.get('name') or path or ref).strip() or (path or ref),
            'kind': str(item.get('kind') or '').strip(),
            'mime_type': str(item.get('mime_type') or item.get('mimeType') or '').strip(),
            'path': path,
            'ref': ref,
        }
        ordered.append({key: value for key, value in entry.items() if value})
    return ordered


def _render_name_list(items: list[str] | None) -> str:
    names = _normalized_name_list(items)
    if not names:
        return 'none'
    return ', '.join(f'`{name}`' for name in names)


def _render_candidate_tool_section(items: list[dict[str, str]] | None, declared_names: list[str] | None) -> list[str]:
    """候选行的判据与文本都在 `render_candidate_tool_line`，两车道共用、字节同形。
    工具说明由 `tools[]` 的 `function.description` 承载（见 `_provider_visible_tool_contract`）。
    """
    return render_candidate_tool_line(
        [str(item.get('tool_id') or '') for item in list(_normalized_candidate_tool_items(items) or [])],
        declared_names,
    )


def _render_repair_required_tool_section(items: list[dict[str, str]] | None) -> list[str]:
    """只列条目。处置指令是回合内常量：前门稳定提示词只留信号识别、重试禁令与缺口申报，
    排查流程住 skill `repair-tool`；节点道住 `shared_repair_required.md`。
    """
    normalized_items = _normalized_repair_required_tool_items(items)
    if not normalized_items:
        return []
    lines = ['repair_required_tools:']
    for item in normalized_items:
        tool_id = str(item.get('tool_id') or '').strip()
        description = str(item.get('description') or '').strip()
        reason = str(item.get('reason') or '').strip()
        detail = description if description else 'No description available.'
        if reason:
            detail = f'{detail} Reason: {reason}'
        lines.append(f'- `{tool_id}`: {detail}')
    return lines


def _render_attachment_reopen_target_section(items: list[dict[str, str]] | None) -> list[str]:
    """只列句柄条目。七句固定规则与 `ceo_frontdoor.md`「异步任务的文件依赖」那组条款同义，
    留两份就是每跳重付一遍；规则的唯一归属是稳定提示词。
    """
    normalized_items = _normalized_attachment_reopen_targets(items)
    if not normalized_items:
        return []
    lines = ['attachment_reopen_targets:']
    for item in normalized_items:
        name = str(item.get('name') or '').strip() or 'attachment'
        kind = str(item.get('kind') or '').strip() or 'file'
        mime_type = str(item.get('mime_type') or '').strip() or 'application/octet-stream'
        details = [f'kind={kind}', f'mime_type={mime_type}']
        path = str(item.get('path') or '').strip()
        ref = str(item.get('ref') or '').strip()
        if path:
            details.append(f'path={path}')
        if ref:
            details.append(f'ref={ref}')
        lines.append(f'- `{name}`: ' + '; '.join(details))
    return lines


def _render_exec_runtime_policy(exec_runtime_policy: dict[str, Any] | None) -> str:
    payload = dict(exec_runtime_policy or {})
    if not payload:
        return 'exec_runtime_policy: none'
    # 延迟导入：tool_contract 会被 main.runtime 反向导入，模块级 import 会成环。
    from main.governance.exec_tool_policy import render_exec_runtime_policy_body

    body = render_exec_runtime_policy_body(payload)
    return 'exec_runtime_policy: ' + (body or 'none')


def _contract_revision_line(payload: dict[str, Any]) -> str:
    return f'contract_revision: {str(payload.get("contract_revision") or "").strip() or "none"}'


def _render_frontdoor_contract_summary(payload: dict[str, Any]) -> str:
    """回合内常量部分：候选工具、待修复工具、附件句柄、临时目录、执行策略。

    省略判定是**逐段**的，不看"钉住块是否存在"这种整体开关：头部文本里出现了哪一段，尾块
    才省哪一段。整体开关会在装配点缺某项输入时（例如某条路径不传 `session_temp_dir`）把那条
    声明整段变没——省缓存省掉一条声明，比不省严重得多。缺的那段照旧在本块出现，宁可重复
    不可缺失。

    skill 名单不再进尾块：头部钉住块带全量名单（id|名称|描述），重印点之间新变为可读
    的 skill 由一次性持久提示落进历史（`g3ku/runtime/skill_roster_notice.py`）——
    投一次就不再每跳重印，压缩那一跳按台账把它带过压缩产物。
    """
    candidate_tools = _normalized_candidate_tool_items(payload.get('candidate_tools'))
    repair_required_tools = _normalized_repair_required_tool_items(payload.get('repair_required_tools'))
    attachment_reopen_targets = _normalized_attachment_reopen_targets(payload.get('attachment_reopen_targets'))
    pinned_text = str(payload.get('pinned_contract_text') or '').strip()
    pinned_exec = 'exec_runtime_policy' in pinned_text
    pinned_temp = 'session_temp_dir:' in pinned_text
    pinned_anchor = 'path_anchor:' in pinned_text
    lines = [
        FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING,
        f'kind: {FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND}',
        _contract_revision_line(payload),
    ]
    lines.extend(
        [
            *_render_attachment_reopen_target_section(attachment_reopen_targets),
            *_render_candidate_tool_section(candidate_tools, payload.get('declared_tool_names')),
            *_render_repair_required_tool_section(repair_required_tools),
        ]
    )
    denied_tool_names = _normalized_name_list(payload.get('denied_tool_names'))
    if denied_tool_names:
        lines.append(
            'denied_tools (`tools[]` 里带着参数表，但这一跳调不动——无权限或资源已下架，'
            f'调用一律被拒；不要反复重试或改着名字试): {_render_name_list(denied_tool_names)}'
        )
    if not pinned_exec:
        lines.append(_render_exec_runtime_policy(payload.get('exec_runtime_policy')))
    if not pinned_temp:
        lines.extend(_render_session_temp_dir(payload.get('session_temp_dir')))
    if not pinned_anchor:
        lines.extend(_render_path_anchor())
    return '\n'.join(lines)


def _render_path_anchor() -> list[str]:
    """落点合同的唯一渲染点：与节点道 `runtime_environment.path_policy` 同一函数、同一句规则，
    两条车道不允许各自措辞。token 集由 `path_anchor_tokens()` 给，实际可用性以解析器为准。
    """
    return [
        f'path_anchor: {path_anchor_tokens()}',
        f'path_anchor_rule: {PATH_ANCHOR_RULE_TEXT}',
    ]


def _render_session_temp_dir(session_temp_dir: Any) -> list[str]:
    """只给路径；规则本体在基础提示词 `ceo_frontdoor.md`「临时文件与中间产物」那条，
    它反过来写"以 runtime tool contract 中 `session_temp_dir` 给出的绝对路径为准"。
    """
    text = str(session_temp_dir or '').strip()
    if not text:
        return []
    return [f'session_temp_dir: {text}']


@dataclass(slots=True)
class FrontdoorToolContract:
    callable_tool_names: list[str]
    candidate_tool_names: list[str]
    visible_skill_ids: list[str]
    candidate_skill_ids: list[str]
    rbac_visible_tool_names: list[str]
    rbac_visible_skill_ids: list[str]
    contract_revision: str
    candidate_tool_items: list[dict[str, str]] | None = None
    candidate_skill_items: list[dict[str, str]] | None = None
    repair_required_tool_items: list[dict[str, str]] | None = None
    repair_required_skill_items: list[dict[str, str]] | None = None
    exec_runtime_policy: dict[str, Any] | None = None
    attachment_reopen_targets: list[dict[str, str]] | None = None
    denied_tool_names: list[str] | None = None
    declared_tool_names: list[str] | None = None
    session_temp_dir: str | None = None
    pinned_contract_text: str | None = None
    pinned_skill_ids: list[str] | None = None

    def to_message_payload(self) -> dict[str, Any]:
        payload = {
            'message_type': FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND,
            'callable_tool_names': list(self.callable_tool_names),
            'denied_tool_names': _normalized_name_list(list(self.denied_tool_names or [])),
            'declared_tool_names': _normalized_name_list(list(self.declared_tool_names or [])),
            'candidate_tools': _normalized_candidate_tool_items(
                list(self.candidate_tool_items or []),
                fallback_names=list(self.candidate_tool_names),
            ),
            'candidate_skill_ids': list(self.candidate_skill_ids),
            'contract_revision': str(self.contract_revision or '').strip(),
            'exec_runtime_policy': (
                dict(self.exec_runtime_policy)
                if isinstance(self.exec_runtime_policy, dict)
                else None
            ),
            'session_temp_dir': str(self.session_temp_dir or '').strip() or None,
        }
        attachment_reopen_targets = _normalized_attachment_reopen_targets(self.attachment_reopen_targets)
        if attachment_reopen_targets:
            payload['attachment_reopen_targets'] = attachment_reopen_targets
        repair_required_tools = _normalized_repair_required_tool_items(self.repair_required_tool_items)
        if repair_required_tools:
            payload['repair_required_tools'] = repair_required_tools
        repair_required_skills = _normalized_repair_required_skill_items(self.repair_required_skill_items)
        if repair_required_skills:
            payload['repair_required_skills'] = repair_required_skills
        pinned_text = str(self.pinned_contract_text or '').strip()
        if pinned_text:
            payload['pinned_contract_text'] = pinned_text
            payload['pinned_skill_ids'] = _normalized_name_list(self.pinned_skill_ids)
        return payload

    def to_message(self) -> dict[str, Any]:
        payload = self.to_message_payload()
        # user 角色：严格 OpenAI-compatible 网关只允许一份 system 且必须在首位，而这两份
        # 块排在请求末位，用 system 会被那些网关 400 拒掉。也不许用 assistant——模型会把
        # 它当成"自己上一轮说过/发给用户的消息"，进而在事后复盘时改写时间线。
        return {
            'role': 'user',
            'content': _render_frontdoor_contract_summary(payload),
            FRONTDOOR_DYNAMIC_TOOL_CONTRACT_PAYLOAD_KEY: payload,
        }


def _frontdoor_tool_contract_payload_from_content(content: Any) -> dict[str, Any] | None:
    payload: dict[str, Any] | None = None
    if isinstance(content, dict):
        payload = dict(content)
    elif isinstance(content, str):
        text = str(content or '').strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except Exception:
            return None
        if isinstance(parsed, dict):
            payload = dict(parsed)
    if not isinstance(payload, dict):
        return None
    if str(payload.get('message_type') or '').strip() != FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND:
        return None
    return payload


def _frontdoor_message_declares_tool_calls(message: dict[str, Any] | None) -> bool:
    if not isinstance(message, dict):
        return False
    for tool_call in list(message.get('tool_calls') or []):
        if isinstance(tool_call, dict):
            return True
    function_call = message.get('function_call')
    if isinstance(function_call, dict) and str(function_call.get('name') or '').strip():
        return True
    return False


def frontdoor_tool_contract_payload_from_message(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    # 运行时注入的契约消息从不携带工具调用。带 tool_calls 的 assistant 消息
    # 是模型回合本身——即使其文本回显了契约抬头/契约 JSON——也不得判为契约
    # 消息，否则该回合会被整体剥离，留下孤儿工具结果。
    if _frontdoor_message_declares_tool_calls(message):
        return None
    payload = message.get(FRONTDOOR_DYNAMIC_TOOL_CONTRACT_PAYLOAD_KEY)
    if isinstance(payload, dict):
        resolved = dict(payload)
        if str(resolved.get('message_type') or '').strip() == FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND:
            return resolved
    return _frontdoor_tool_contract_payload_from_content((message or {}).get('content'))


def build_frontdoor_tool_contract(
    *,
    callable_tool_names: list[str] | None,
    candidate_tool_names: list[str] | None,
    candidate_tool_items: list[dict[str, str]] | None = None,
    visible_skill_ids: list[str] | None = None,
    candidate_skill_ids: list[str] | None = None,
    candidate_skill_items: list[dict[str, str]] | None = None,
    repair_required_tool_items: list[dict[str, str]] | None = None,
    repair_required_skill_items: list[dict[str, str]] | None = None,
    rbac_visible_tool_names: list[str] | None = None,
    rbac_visible_skill_ids: list[str] | None = None,
    contract_revision: str | None = None,
    exec_runtime_policy: dict[str, Any] | None = None,
    attachment_reopen_targets: list[dict[str, str]] | None = None,
    session_temp_dir: str | None = None,
    denied_tool_names: list[str] | None = None,
    declared_tool_names: list[str] | None = None,
    pinned_contract_text: str | None = None,
    pinned_skill_ids: list[str] | None = None,
) -> FrontdoorToolContract:
    callable_names = _normalized_name_list(callable_tool_names)
    candidate_names = [
        name
        for name in _normalized_name_list(candidate_tool_names)
        if name not in set(callable_names)
    ]
    return FrontdoorToolContract(
        callable_tool_names=callable_names,
        candidate_tool_names=candidate_names,
        visible_skill_ids=_normalized_name_list(visible_skill_ids),
        candidate_skill_ids=_normalized_name_list(candidate_skill_ids),
        rbac_visible_tool_names=_normalized_name_list(rbac_visible_tool_names),
        rbac_visible_skill_ids=_normalized_name_list(rbac_visible_skill_ids),
        contract_revision=str(contract_revision or '').strip(),
        candidate_tool_items=_normalized_candidate_tool_items(candidate_tool_items, fallback_names=candidate_names),
        candidate_skill_items=list(candidate_skill_items or []),
        repair_required_tool_items=_normalized_repair_required_tool_items(repair_required_tool_items),
        repair_required_skill_items=_normalized_repair_required_skill_items(repair_required_skill_items),
        exec_runtime_policy=dict(exec_runtime_policy) if isinstance(exec_runtime_policy, dict) else None,
        attachment_reopen_targets=_normalized_attachment_reopen_targets(attachment_reopen_targets),
        session_temp_dir=str(session_temp_dir or '').strip() or None,
        denied_tool_names=_normalized_name_list(denied_tool_names),
        declared_tool_names=_normalized_name_list(declared_tool_names),
        pinned_contract_text=str(pinned_contract_text or '').strip() or None,
        pinned_skill_ids=_normalized_name_list(pinned_skill_ids),
    )


def _runtime_appendix_pair(heading: str) -> str:
    return {
        FRONTDOOR_DYNAMIC_TOOL_CONTRACT_HEADING: FRONTDOOR_DYNAMIC_TOOL_CONTRACT_KIND,
    }[heading]


def is_frontdoor_tool_contract_message(message: dict[str, Any]) -> bool:
    """Whether a message is the runtime-injected appendix block."""
    if frontdoor_tool_contract_payload_from_message(message) is not None:
        return True
    if _frontdoor_message_declares_tool_calls(message):
        return False
    # 注入的契约消息经 sanitize 后只保留 role/content，payload 键被剥离，需靠抬头识别。
    # 契约现在以 user 角色注入（线体只许一份首位 system），存量记录里仍是 system 角色，
    # 模型回显的契约残留是 assistant 文本——三种角色都识别，迁移期不得因认不出旧块而
    # 让尾部叠出第二份。抬头前缀本身足够特异，真实用户消息不会以它开头。
    if str((message or {}).get('role') or '').strip().lower() not in {'assistant', 'system', 'user'}:
        return False
    content = str((message or {}).get('content') or '').strip()
    return any(content.startswith(heading) for heading in RUNTIME_APPENDIX_HEADINGS)


def _frontdoor_tool_contract_heading_index(text: str) -> int:
    """Return the start of a rendered appendix block embedded in ``text``.

    The heading alone is not enough to classify ordinary user prose that
    happens to mention the contract.  Requiring the canonical kind marker in
    the nearby suffix keeps this helper focused on the provider-facing blocks
    that the runtime injects.
    """
    normalized = str(text or '')
    best = -1
    for heading in RUNTIME_APPENDIX_HEADINGS:
        heading_index = normalized.find(heading)
        if heading_index < 0:
            continue
        suffix = normalized[heading_index : heading_index + 512]
        if f'kind: {_runtime_appendix_pair(heading)}' not in suffix:
            continue
        if best < 0 or heading_index < best:
            best = heading_index
    return best


def is_frontdoor_tool_contract_echo_text(text: Any) -> bool:
    """Whether model/channel text is a standalone injected appendix-block echo."""
    normalized = str(text or '').strip()
    if not normalized:
        return False
    if any(normalized.startswith(heading) for heading in RUNTIME_APPENDIX_HEADINGS):
        return _frontdoor_tool_contract_heading_index(normalized) == 0
    return is_frontdoor_tool_contract_message({'role': 'assistant', 'content': normalized})


def strip_frontdoor_tool_contract_echo(text: Any) -> str:
    """Remove a rendered runtime appendix-block echo from user-facing text.

    A standalone echo becomes empty.  If a model puts a visible answer before
    the echoed block, preserve that answer and remove only the internal suffix.
    JSON contract payloads are handled by the standalone classifier above.
    """
    normalized = str(text or '')
    if is_frontdoor_tool_contract_echo_text(normalized):
        return ''
    heading_index = _frontdoor_tool_contract_heading_index(normalized)
    if heading_index >= 0:
        return normalized[:heading_index].strip()
    return normalized.strip()


def upsert_frontdoor_tool_contract_message(
    messages: list[dict[str, Any]] | None,
    contract: FrontdoorToolContract,
) -> list[dict[str, Any]]:
    """剥离历史里的旧合同块，不再追加新尾块。

    尾块已退役：静态声明在头部快照，变化由一次性 `[RUNTIME_NOTICE]` 投递。保留函数是为了
    所有既有装配点零改动地停在"只剥离"语义上；`contract` 参数只为兼容调用方。
    """
    _ = contract
    return [
        dict(message)
        for message in list(messages or [])
        if isinstance(message, dict) and not is_frontdoor_tool_contract_message(message)
    ]
