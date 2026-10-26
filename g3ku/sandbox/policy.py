"""沙箱/只读档的执行策略（P0 的单一判据来源）。

契约 owner：``docs/architecture/sandbox-execution-plane.md``。

会话只存**一档意图**（``mode``），实际执行面由**安装态**解析：

===============  ======================  ==========================
安装态           ``restricted``          ``unrestricted``
===============  ======================  ==========================
未安装 / partial  只读档 ``readonly``       宿主全权限 ``host_full``
ready           沙箱档 ``jailed``          宿主全权限 ``host_full``
===============  ======================  ==========================

装/卸沙箱因此**不需要改任何会话元数据**：意图不动，档位随安装态变。P0 阶段还没有
jail，安装态恒为 ``not_installed``，所以只可能解析出 ``readonly`` 或 ``host_full``。

两档只读的强度不同，这点不能混：

* **third_party 只读** 是安全边界，用**白名单**（未登记的名字一律拒）。新工具默认
  拿不到宿主访问权，只有显式登记 + 声明读什么根才能进。
* **owner 只读** 是防手滑，用**黑名单**（写类/进程/任务/外网写入工具拒，其余照旧）。

只读档约束的是**数据面**（文件 / 进程 / 网络）。控制面对 owner 照旧按 RBAC，
对 third_party 本来就全拒。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from g3ku.runtime.session_trust import (
    TRUST_OWNER,
    normalize_trust,
    session_trust,
)

TIER_HOST_FULL = "host_full"
TIER_READONLY = "readonly"
TIER_JAILED = "jailed"

MODE_RESTRICTED = "restricted"
MODE_UNRESTRICTED = "unrestricted"

INSTALL_NOT_INSTALLED = "not_installed"
INSTALL_RUNTIME_MISSING = "runtime_missing"
INSTALL_PARTIAL = "partial"
INSTALL_READY = "ready"

ALL_TIERS: tuple[str, ...] = (TIER_HOST_FULL, TIER_READONLY, TIER_JAILED)

# jail 后端（bwrap / seatbelt / wsl_bwrap / container 的 manager + router）接进执行道
# 之前，`jailed` 一律不发放：安装态可以是 `ready`，但没有后端就意味着没有隔离，
# 这时把会话放进 jailed 等于给它开数据面而没有人看守。P1a 落地时改这一处。
JAIL_BACKENDS_AVAILABLE = False

# third_party 只读档的白名单。**加工具要连"它读什么根"一起声明**：
# 名单里的读类工具都受 `fs_read_roots` 约束（见 `g3ku/utils/path_roots.py`）。
READONLY_THIRD_PARTY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # 无副作用的常驻信号
        "silent",
        "time",
        "load_tool_context",
        "load_tool_context_v2",
        "load_skill_context",
        "load_skill_context_v2",
        # 读类数据面（受读根限定）
        "content_open",
        "content_search",
        "content_describe",
        "filesystem_stat",
        "view_message_history",
        # 贴纸目录只读扫描：群聊里可用的表达工具，不碰文件与网络
        "search_stickers",
    }
)

WRITE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "filesystem_write",
        "filesystem_edit",
        "filesystem_copy",
        "filesystem_move",
        "filesystem_delete",
        "filesystem_propose_patch",
    }
)

# 会写出工作区或联网的工具。third_party 只读档靠白名单天然拦住；这里列出来是给
# owner 只读档用的。
NETWORK_WRITE_TOOL_NAMES: frozenset[str] = frozenset({"skill-installer"})
NETWORK_TOOL_NAMES: frozenset[str] = frozenset({"web_fetch"})
PROCESS_TOOL_NAMES: frozenset[str] = frozenset({"exec", "process_kill"})
_TASK_TOOL_PREFIX = "task_"
TASK_TOOL_NAMES: frozenset[str] = frozenset(
    {"create_async_task", "send_message", "schedule_wake", "cron"}
)

# owner 只读档：写类 / 进程控制 / 任务族 / 会写盘的联网工具一律拒。
_OWNER_READONLY_DENIED: frozenset[str] = (
    WRITE_TOOL_NAMES
    | NETWORK_WRITE_TOOL_NAMES
    | frozenset({"process_kill"})
    | TASK_TOOL_NAMES
)


def is_task_tool_name(tool_name: Any) -> bool:
    """任务族判定按前缀 + 点名：`task_*` 家族有十几个名字，逐个数会漏。"""
    name = str(tool_name or "").strip()
    if not name:
        return False
    return name in TASK_TOOL_NAMES or name.startswith(_TASK_TOOL_PREFIX)


def default_mode_for_trust(trust: Any) -> str:
    """群聊/未列名私聊默认受限；操作员自己的会话默认全权限（与升级前一致）。"""
    return MODE_UNRESTRICTED if normalize_trust(trust) == TRUST_OWNER else MODE_RESTRICTED


def resolve_tier(
    *,
    trust: Any,
    mode: Any,
    install_state: Any,
    jail_available: bool | None = None,
) -> str:
    """意图 × 安装态 → 实际执行面。"""
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode != MODE_RESTRICTED:
        return TIER_HOST_FULL
    if jail_available is None:
        jail_available = JAIL_BACKENDS_AVAILABLE
    if not jail_available:
        # 后端没接进执行道：安装态再好看也只给只读档，绝不发放无人看守的数据面。
        return TIER_READONLY
    install = str(install_state or "").strip().lower() or INSTALL_NOT_INSTALLED
    if install == INSTALL_READY:
        return TIER_JAILED
    # partial 只服务 owner：third_party 的必需维度（fs_read/fs_write/network）不全时
    # 绝不把它放进 jail——那会把"缺一维"当"全缺"或反过来，两种都是假安全。
    if install == INSTALL_PARTIAL and normalize_trust(trust) == TRUST_OWNER:
        return TIER_JAILED
    return TIER_READONLY


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    session_key: str
    trust: str
    mode: str
    install_state: str
    tier: str

    @property
    def restricted(self) -> bool:
        return self.tier != TIER_HOST_FULL

    @property
    def allowlist(self) -> frozenset[str] | None:
        """None = 不设白名单（宿主全权限 / owner 只读走黑名单）。"""
        if self.tier == TIER_READONLY and self.trust != TRUST_OWNER:
            return READONLY_THIRD_PARTY_TOOL_NAMES
        return None

    @property
    def denied(self) -> frozenset[str]:
        if self.tier == TIER_READONLY and self.trust == TRUST_OWNER:
            return _OWNER_READONLY_DENIED
        if self.tier == TIER_JAILED:
            # 沙箱档数据面放开，但任务仍然不许派：只读意图传不进 worker 执行面。
            return TASK_TOOL_NAMES
        return frozenset()

    @property
    def exec_requires_governed(self) -> bool:
        """owner 只读档的 exec 必须落到 governed（形态守卫 + 路径策略 + 工作区监禁）。"""
        return self.tier == TIER_READONLY and self.trust == TRUST_OWNER

    def tool_allowed(self, tool_name: Any) -> bool:
        name = str(tool_name or "").strip()
        if not name:
            return False
        if is_task_tool_name(name) and self.restricted:
            return False
        if name in self.denied:
            return False
        allowlist = self.allowlist
        if allowlist is not None and name not in allowlist:
            return False
        return True

    def blocked_tool_names(self, names: Any) -> list[str]:
        return [str(name) for name in list(names or []) if not self.tool_allowed(name)]


def session_policy(
    session_key: Any,
    *,
    mode: Any = None,
    install_state: Any = None,
) -> SessionPolicy:
    """按会话键解析策略。模式与安装态都走进程内缓存，这里没有磁盘 I/O。

    空会话键 = 没有会话上下文（测试装配、CLI、内部检查），按不受限处理。这是安全的：
    外部渠道的每一轮都必然带 ``ext:`` 键，走不到这一支；而把它按第三方处理会让
    所有无会话的装配路径突然失去工具，收益为零、破坏面很大。
    """
    raw_key = str(session_key or "").strip()
    if not raw_key:
        return SessionPolicy(
            session_key="",
            trust=TRUST_OWNER,
            mode=MODE_UNRESTRICTED,
            install_state=str(install_state or "").strip().lower() or INSTALL_NOT_INSTALLED,
            tier=TIER_HOST_FULL,
        )
    trust = session_trust(raw_key)
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {MODE_RESTRICTED, MODE_UNRESTRICTED}:
        normalized_mode = _stored_mode(raw_key) or default_mode_for_trust(trust)
    install = str(install_state or "").strip().lower() or _probed_install_state()
    return SessionPolicy(
        session_key=raw_key,
        trust=trust,
        mode=normalized_mode,
        install_state=install,
        tier=resolve_tier(trust=trust, mode=normalized_mode, install_state=install),
    )


def _stored_mode(session_key: str) -> str:
    # 惰性导入：`state` 依赖本模块的常量，模块级导入会成环。
    try:
        from g3ku.sandbox.state import get_session_mode
    except Exception:  # noqa: BLE001
        return ""
    try:
        return get_session_mode(session_key)
    except Exception:  # noqa: BLE001
        return ""


def _probed_install_state() -> str:
    try:
        from g3ku.sandbox.state import install_state_name
    except Exception:  # noqa: BLE001
        return INSTALL_NOT_INSTALLED
    try:
        return install_state_name()
    except Exception:  # noqa: BLE001
        return INSTALL_NOT_INSTALLED


__all__ = [
    "ALL_TIERS",
    "INSTALL_NOT_INSTALLED",
    "INSTALL_PARTIAL",
    "INSTALL_READY",
    "INSTALL_RUNTIME_MISSING",
    "MODE_RESTRICTED",
    "MODE_UNRESTRICTED",
    "NETWORK_TOOL_NAMES",
    "NETWORK_WRITE_TOOL_NAMES",
    "PROCESS_TOOL_NAMES",
    "READONLY_THIRD_PARTY_TOOL_NAMES",
    "SessionPolicy",
    "TASK_TOOL_NAMES",
    "TIER_HOST_FULL",
    "TIER_JAILED",
    "TIER_READONLY",
    "WRITE_TOOL_NAMES",
    "default_mode_for_trust",
    "is_task_tool_name",
    "resolve_tier",
    "session_policy",
]
