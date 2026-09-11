"""exec 命令白名单与操作者审批服务（安全守卫的放行通道）。

设计约束（评审结论，勿破坏）：

- **放行必须经人**：模型不能通过任何参数/标志自我豁免；唯一形状是
  「拦截 → 操作者批准」。批准动作只有管理端 API 可达。
- **粒度是命令模板**：条目存储为归一化锚定模板（空白 → ``\\s+``、``*``
  通配、其余字面转义），匹配整条归一化命令，不做子串包含——避免
  ``rm`` 关键词放行 ``rm -rf /`` 的反模式。
- **豁免只作用于 deny 层**：工作区路径监禁等前置层永不豁免（由
  ExecTool 的检查顺序保证）。
- **跨进程**：任务在 worker 进程执行、管理台在 web 进程裁决，审批
  请求与决定持久化在 governance sqlite，等待方轮询状态而非内存 Future。
- **防刷屏**：同一命令近窗内连续超时/拒绝达到阈值后不再发起等待，
  直接拒绝（防注入诱导的审批轰炸拖慢车道）。

白名单条目与审批等待时长存 ``governance_meta``（JSON），审批请求走
``exec_command_approvals`` 表；两者的持久化原语都在 GovernanceStore。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from typing import Any

from loguru import logger

from main.governance.store import GovernanceStore
from main.protocol import now_iso

WHITELIST_META_KEY = 'exec_command_whitelist'
APPROVAL_WAIT_META_KEY = 'exec_approval_wait_seconds'

DEFAULT_APPROVAL_WAIT_SECONDS = 120.0
MIN_APPROVAL_WAIT_SECONDS = 5.0
MAX_APPROVAL_WAIT_SECONDS = 3600.0
MAX_WHITELIST_ENTRIES = 200
# 模板去掉空白与通配符后剩余的"字面字符数"下限；低于它视为过宽
# （如 ``*``、``rm`` 单关键词），拒绝入库。
MIN_TEMPLATE_LITERAL_CHARS = 3
# 防刷屏：近窗内同一命令连续 expired/denied 达到该次数后不再等待。
APPROVAL_FAST_REJECT_THRESHOLD = 2
APPROVAL_FAST_REJECT_WINDOW_MINUTES = 10.0

WHITELIST_SCOPES = ('all', 'ceo', 'tasks')
APPROVAL_DECISIONS = ('approved_once', 'approved_whitelist', 'denied')
_TERMINAL_APPROVAL_STATUSES = frozenset({'approved_once', 'approved_whitelist', 'denied', 'expired', 'cancelled'})


def normalize_command(command: str) -> str:
    """空白归一化：首尾去空、连续空白折叠为单空格。"""
    return re.sub(r'\s+', ' ', str(command or '').strip())


def template_to_regex_src(pattern: str) -> str:
    """命令模板 → 锚定正则源码。

    规则：连续空白 → ``\\s+``；``*`` → ``.*``；其余字符字面转义。
    调用方应以 ``re.fullmatch`` 语义使用（本函数返回已带 ^...$ 锚定）。
    """
    normalized = normalize_command(pattern)
    if not normalized:
        raise ValueError('empty pattern')
    rendered_segments: list[str] = []
    for segment in normalized.split('*'):
        tokens = re.split(r'(\s+)', segment)
        rendered_segments.append(
            ''.join(r'\s+' if token.isspace() else re.escape(token) for token in tokens if token)
        )
    return '^' + '.*'.join(rendered_segments) + '$'


def compile_command_template(pattern: str) -> re.Pattern[str]:
    return re.compile(template_to_regex_src(pattern), re.IGNORECASE)


def template_literal_chars(pattern: str) -> int:
    return len(re.sub(r'[\s*]', '', normalize_command(pattern)))


def is_pattern_too_broad(pattern: str) -> bool:
    try:
        if template_literal_chars(pattern) < MIN_TEMPLATE_LITERAL_CHARS:
            return True
        compile_command_template(pattern)
    except (ValueError, re.error):
        return True
    return False


def scope_allows(entry_scope: str, actor_role: str) -> bool:
    """条目作用域判定：all 通用；ceo 仅主 agent 会话；tasks 仅任务节点。"""
    scope = str(entry_scope or 'all').strip().lower()
    role = str(actor_role or '').strip().lower()
    if scope == 'all':
        return True
    if scope == 'ceo':
        return role == 'ceo'
    if scope == 'tasks':
        return role in {'execution', 'inspection'}
    return False


class ExecApprovalService:
    """白名单 CRUD + 审批请求/轮询/裁决。绑定一个 GovernanceStore。"""

    def __init__(self, store: GovernanceStore):
        self._store = store

    # -- 白名单 ---------------------------------------------------------

    def list_whitelist(self) -> list[dict[str, Any]]:
        raw = self._store.get_meta(WHITELIST_META_KEY)
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning('exec whitelist meta is not valid JSON; treating as empty')
            return []
        return [dict(item) for item in data if isinstance(item, dict)]

    def _save_whitelist(self, entries: list[dict[str, Any]]) -> None:
        self._store.set_meta(WHITELIST_META_KEY, json.dumps(entries, ensure_ascii=False, indent=2))

    def add_whitelist_entry(
        self,
        *,
        pattern: str,
        scope: str = 'all',
        created_by: str = 'operator',
        reason: str = '',
        source_command: str = '',
    ) -> dict[str, Any]:
        normalized_pattern = normalize_command(pattern)
        if not normalized_pattern:
            raise ValueError('pattern_required')
        try:
            compile_command_template(normalized_pattern)
        except (ValueError, re.error) as exc:
            raise ValueError(f'pattern_invalid: {exc}') from exc
        if is_pattern_too_broad(normalized_pattern):
            raise ValueError(
                f'pattern_too_broad: 模板字面字符少于 {MIN_TEMPLATE_LITERAL_CHARS} 或不可解析，'
                '放行粒度过粗（等价于关闭守卫）'
            )
        normalized_scope = str(scope or 'all').strip().lower()
        if normalized_scope not in WHITELIST_SCOPES:
            raise ValueError(f'scope_invalid: {scope}')
        entries = self.list_whitelist()
        for item in entries:
            if str(item.get('pattern') or '') == normalized_pattern and str(item.get('scope') or 'all') == normalized_scope:
                raise ValueError('pattern_already_exists')
        if len(entries) >= MAX_WHITELIST_ENTRIES:
            raise ValueError(f'whitelist_full: 上限 {MAX_WHITELIST_ENTRIES} 条，请先删除不再需要的条目')
        entry = {
            'pattern': normalized_pattern,
            'scope': normalized_scope,
            'created_by': str(created_by or 'operator'),
            'created_at': now_iso(),
            'reason': str(reason or '').strip(),
            'source_command': str(source_command or '').strip(),
        }
        entries.append(entry)
        self._save_whitelist(entries)
        return entry

    def remove_whitelist_entry(self, *, pattern: str, scope: str = 'all') -> bool:
        normalized_pattern = normalize_command(pattern)
        normalized_scope = str(scope or 'all').strip().lower()
        entries = self.list_whitelist()
        remaining = [
            item
            for item in entries
            if not (
                str(item.get('pattern') or '') == normalized_pattern
                and str(item.get('scope') or 'all') == normalized_scope
            )
        ]
        if len(remaining) == len(entries):
            return False
        self._save_whitelist(remaining)
        return True

    def command_allowed(self, command: str, *, actor_role: str = '') -> dict[str, Any] | None:
        """命中白名单则返回条目，否则 None。匹配整条归一化命令（锚定）。"""
        normalized = normalize_command(command)
        if not normalized:
            return None
        for entry in self.list_whitelist():
            if not scope_allows(str(entry.get('scope') or 'all'), actor_role):
                continue
            pattern = str(entry.get('pattern') or '')
            if not pattern:
                continue
            try:
                compiled = compile_command_template(pattern)
            except (ValueError, re.error):
                logger.warning('exec whitelist entry has invalid pattern; skipped: {!r}', pattern)
                continue
            if compiled.fullmatch(normalized):
                return entry
        return None

    # -- 审批等待时长设置 --------------------------------------------------

    def get_approval_wait_seconds(self) -> float:
        raw = self._store.get_meta(APPROVAL_WAIT_META_KEY)
        try:
            value = float(str(raw or '').strip())
        except (TypeError, ValueError):
            return DEFAULT_APPROVAL_WAIT_SECONDS
        return min(MAX_APPROVAL_WAIT_SECONDS, max(MIN_APPROVAL_WAIT_SECONDS, value))

    def set_approval_wait_seconds(self, seconds: float) -> float:
        clamped = min(MAX_APPROVAL_WAIT_SECONDS, max(MIN_APPROVAL_WAIT_SECONDS, float(seconds or 0)))
        self._store.set_meta(APPROVAL_WAIT_META_KEY, str(clamped))
        return clamped

    # -- 审批请求 / 轮询 / 裁决 --------------------------------------------

    def recent_rejections_for(self, command: str) -> int:
        """近窗内同一命令 expired/denied/cancelled 的次数（防刷屏依据）。"""
        cutoff = _iso_minutes_ago(APPROVAL_FAST_REJECT_WINDOW_MINUTES)
        return self._store.count_recent_exec_approval_outcomes(
            normalize_command(command),
            since_iso=cutoff,
            statuses=('expired', 'denied', 'cancelled'),
        )

    def request_approval(
        self,
        *,
        command: str,
        guard_reason: str,
        actor_role: str,
        lane: str,
        context_id: str,
        cwd: str = '',
        wait_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """登记待审批请求。近窗连续未获批达阈值时返回 None（快拒，不再等待）。"""
        self._store.expire_stale_exec_approvals(_iso_minutes_ago(MAX_APPROVAL_WAIT_SECONDS / 60.0 + 5.0))
        if self.recent_rejections_for(command) >= APPROVAL_FAST_REJECT_THRESHOLD:
            return None
        effective_wait = float(wait_seconds if wait_seconds is not None else self.get_approval_wait_seconds())
        approval_id = uuid.uuid4().hex
        created_at = now_iso()
        self._store.create_exec_approval(
            {
                'approval_id': approval_id,
                'command_norm': normalize_command(command),
                'command_text': str(command or '')[:4000],
                'cwd': str(cwd or ''),
                'guard_reason': str(guard_reason or ''),
                'actor_role': str(actor_role or ''),
                'lane': str(lane or ''),
                'context_id': str(context_id or ''),
                'status': 'pending',
                'created_at': created_at,
                'expires_at': _iso_seconds_later(effective_wait),
            }
        )
        self._store.prune_exec_approvals(keep_recent=200)
        return {
            'approval_id': approval_id,
            'wait_seconds': effective_wait,
            'created_at': created_at,
        }

    async def wait_for_decision(
        self,
        approval_id: str,
        *,
        timeout_seconds: float,
        cancel_token: Any | None = None,
        poll_interval_seconds: float = 0.5,
    ) -> str:
        """轮询裁决结果；返回 approved_once/approved_whitelist/denied/expired/cancelled。

        到期未决 → 标记 expired；cancel_token 触发或所在任务被取消 →
        标记 cancelled（CancelledError 由 finally 兜底落状态）。
        """
        deadline = time.monotonic() + max(0.5, float(timeout_seconds))
        try:
            while True:
                record = self._store.get_exec_approval(approval_id)
                status = str((record or {}).get('status') or '')
                if status in _TERMINAL_APPROVAL_STATUSES:
                    return status
                if _cancel_token_triggered(cancel_token):
                    self._store.decide_exec_approval(approval_id, status='cancelled', decided_by='system')
                    return 'cancelled'
                if time.monotonic() >= deadline:
                    self._store.decide_exec_approval(approval_id, status='expired', decided_by='system')
                    return 'expired'
                await asyncio.sleep(max(0.05, float(poll_interval_seconds)))
        except asyncio.CancelledError:
            self._store.decide_exec_approval(approval_id, status='cancelled', decided_by='system')
            raise

    def list_approvals(self, *, status: str | None = 'pending', limit: int = 50) -> list[dict[str, Any]]:
        """管理台用：按状态列审批请求（默认 pending，按创建时间倒序）。"""
        return [dict(item) for item in self._store.list_exec_approvals(status=status, limit=limit)]

    def resolve(
        self,
        approval_id: str,
        *,
        decision: str,
        decided_by: str = 'operator',
        scope: str = 'all',
        reason: str = '',
    ) -> dict[str, Any]:
        """管理端裁决：approve_once / approve_whitelist / deny。

        approve_whitelist 以该请求的归一化命令为模板加白（scope 由调用方
        给定），随后本条请求按 approved_whitelist 放行。
        """
        normalized_decision = str(decision or '').strip().lower()
        mapping = {
            'approve_once': 'approved_once',
            'approve': 'approved_once',
            'approve_whitelist': 'approved_whitelist',
            'deny': 'denied',
            'reject': 'denied',
        }
        status = mapping.get(normalized_decision)
        if status is None:
            raise ValueError(f'decision_invalid: {decision}')
        record = self._store.get_exec_approval(approval_id)
        if record is None:
            raise KeyError(str(approval_id or ''))
        if str(record.get('status') or '') != 'pending':
            return {'approval_id': approval_id, 'status': str(record.get('status') or ''), 'duplicate': True}
        entry: dict[str, Any] | None = None
        if status == 'approved_whitelist':
            pattern = str(record.get('command_norm') or '')
            entry = self.add_whitelist_entry(
                pattern=pattern,
                scope=scope,
                created_by=str(decided_by or 'operator'),
                reason=str(reason or '') or f'approved from request {approval_id}',
                source_command=str(record.get('command_text') or ''),
            )
        decided = self._store.decide_exec_approval(
            approval_id,
            status=status,
            decided_by=str(decided_by or 'operator'),
            decision_scope=str(scope or '') if status == 'approved_whitelist' else '',
        )
        if not decided:
            latest = self._store.get_exec_approval(approval_id) or {}
            return {'approval_id': approval_id, 'status': str(latest.get('status') or ''), 'duplicate': True}
        return {
            'approval_id': approval_id,
            'status': status,
            'duplicate': False,
            'whitelist_entry': entry,
        }


def _cancel_token_triggered(cancel_token: Any | None) -> bool:
    if cancel_token is None:
        return False
    for attr in ('is_cancelled', 'cancelled'):
        flag = getattr(cancel_token, attr, None)
        if callable(flag):
            try:
                return bool(flag())
            except Exception:
                return False
        if flag is not None:
            return bool(flag)
    return False


def _iso_minutes_ago(minutes: float) -> str:
    # 与 main.protocol.now_iso 同格式（本地时区、秒精度），保证 sqlite
    # 字符串比较的时序语义一致。
    from datetime import datetime, timedelta

    return (datetime.now().astimezone() - timedelta(minutes=float(minutes))).isoformat(timespec='seconds')


def _iso_seconds_later(seconds: float) -> str:
    from datetime import datetime, timedelta

    return (datetime.now().astimezone() + timedelta(seconds=float(seconds))).isoformat(timespec='seconds')
