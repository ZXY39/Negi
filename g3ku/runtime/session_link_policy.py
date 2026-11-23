"""会话关联准入判据的单一作者：可关联性、懒建边的信任闸、候选目录。

操作员弹窗（REST）与投递车道（`MainRuntimeService.deliver_session_message`）
共用这一份判据，两边不会漂移。取数走盘上转录清单与 ext 注册表，worker 进程
同样读得到；读不出来一律 fail-closed（不可关联 / 空目录），不建边。

合同 owner：`docs/architecture/session-messaging.md`「关系模型与短标识」。
"""

from __future__ import annotations

from typing import Any

from g3ku.runtime.session_trust import TRUST_OWNER, session_trust

# 候选目录一次最多给这么多条；余量由 truncated 交代。
SESSION_CANDIDATE_LIMIT = 50

# 会话命名空间：寻址层认得它们，可关联性由 session_link_problem 单独裁。
# internal:/china: 解析得出来但不可关联，于是拒收理由是 not_linkable 而不是 missing。
SESSION_ADDRESS_PREFIXES = ('web:', 'ext:', 'internal:', 'china:')


def _session_manager() -> Any:
    from g3ku.runtime.web_ceo_sessions import workspace_path
    from g3ku.session.manager import SessionManager

    return SessionManager(workspace_path())


def _registry() -> Any:
    from g3ku.runtime.external_sessions import get_external_session_registry
    from g3ku.runtime.web_ceo_sessions import workspace_path

    return get_external_session_registry(workspace_path())


def session_link_problem(target: str, *, self_key: str) -> str:
    """一个关联目标的拒绝理由；'' 表示可关联。

    隐藏 ext 条目不候选（操作员已隐藏的先尊重）；`internal:` 隐藏会话与
    `china:` 归档（无投递路径）不可关联；自身不成边。
    """
    key = str(target or '').strip()
    if not key or key == str(self_key or '').strip():
        return 'session_not_linkable'
    if key.startswith('web:'):
        try:
            exists = bool(_session_manager().has_transcript(key))
        except Exception:
            exists = False
        return '' if exists else 'session_missing'
    if key.startswith('ext:'):
        try:
            entry = _registry().get_by_session_key(key)
        except Exception:
            entry = None
        if entry is None:
            return 'session_missing'
        if bool(getattr(entry, 'hidden', False)):
            return 'session_not_linkable'
        return ''
    return 'session_not_linkable'


def may_auto_link(sender: str) -> bool:
    """懒建边只对 owner 信任档开放。

    关联边是 ACL：第三方会话（群 / 陌生人私聊）凭一个 id 就能闯进操作员会话的
    名册并唤醒它，等于把操作员那道批准门拆了。第三方仍要操作员先勾。
    """
    return session_trust(sender) == TRUST_OWNER


def linkable_session_candidates(
    self_key: str, *, limit: int = SESSION_CANDIDATE_LIMIT
) -> tuple[list[dict[str, str]], bool]:
    """可关联候选目录（含尚未关联的）：懒建边要先让模型拿得到 id。

    只读转录清单的头/尾窗与注册表条目，不构造会话对象。按最近活跃倒序，
    超上限截断并置 truncated。
    """
    normalized_self = str(self_key or '').strip()
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        listing = list(_session_manager().list_sessions() or [])
    except Exception:
        listing = []
    for item in listing:
        key = str(item.get('key') or '').strip()
        if not key.startswith('web:') or key == normalized_self or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                'session_key': key,
                'title': str(item.get('title') or '').strip(),
                'updated_at': str(item.get('updated_at') or item.get('created_at') or ''),
            }
        )
    try:
        entries = list(_registry().list_entries() or [])
    except Exception:
        entries = []
    for entry in entries:
        key = str(getattr(entry, 'session_key', '') or '').strip()
        if not key or key == normalized_self or key in seen:
            continue
        if bool(getattr(entry, 'hidden', False)):
            continue
        seen.add(key)
        rows.append(
            {
                'session_key': key,
                'title': str(getattr(entry, 'title', '') or '').strip(),
                'updated_at': str(
                    getattr(entry, 'last_inbound_at', '') or getattr(entry, 'created_at', '') or ''
                ),
            }
        )
    rows.sort(key=lambda row: (row['updated_at'], row['session_key']), reverse=True)
    cap = max(1, int(limit))
    truncated = len(rows) > cap
    return rows[:cap], truncated


__all__ = [
    'SESSION_ADDRESS_PREFIXES',
    'SESSION_CANDIDATE_LIMIT',
    'linkable_session_candidates',
    'may_auto_link',
    'session_link_problem',
]
