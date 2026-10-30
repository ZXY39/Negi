"""沙箱/只读档的执行策略（P0 的单一判据来源）。

契约 owner：``docs/architecture/sandbox-execution-plane.md``。

会话只存**一档意图**（``mode``），实际执行面由**安装态**解析：

===============  ======================  ==========================
安装态           ``restricted``          ``unrestricted``
===============  ======================  ==========================
未安装 / partial  受限档 ``limited``        宿主全权限 ``host_full``
ready           沙箱档 ``jailed``          宿主全权限 ``host_full``
===============  ======================  ==========================

装/卸沙箱因此**不需要改任何会话元数据**：意图不动，档位随安装态变。P0 阶段还没有
jail，安装态恒为 ``not_installed``，所以只可能解析出 ``readonly`` 或 ``host_full``。

两档只读的强度不同，这点不能混：

* **limited 受限档** 用**按会话白名单**：默认集合只放开文件/读类/记忆/调度/网络读取
  与展示类工具；写类工具受会话工作目录写根约束。operator 可以在「允许使用的工具列表」
  里逐会话增删，`exec` 等危险工具也可显式打开（打开后 exec 强制 governed）。
* **owner 旧只读档** 是防手滑，用**黑名单**（写类/进程/任务/外网写入工具拒，其余照旧）。

受限档约束的是**数据面**（文件 / 进程 / 网络）。控制面对 owner 照旧按 RBAC，
对 third_party 本来就全拒。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from g3ku.runtime.session_trust import (
    TRUST_OWNER,
    normalize_trust,
    session_trust,
)

TIER_HOST_FULL = "host_full"
TIER_LIMITED = "limited"
# 兼容旧数据/旧调用：上一版的 `readonly` 档已被 `limited` 取代，保留常量只为读旧快照。
TIER_READONLY = "readonly"
TIER_JAILED = "jailed"

MODE_RESTRICTED = "restricted"
MODE_UNRESTRICTED = "unrestricted"

INSTALL_NOT_INSTALLED = "not_installed"
INSTALL_RUNTIME_MISSING = "runtime_missing"
INSTALL_PARTIAL = "partial"
INSTALL_READY = "ready"

ALL_TIERS: tuple[str, ...] = (TIER_HOST_FULL, TIER_LIMITED, TIER_READONLY, TIER_JAILED)

# jail 后端（bwrap / seatbelt / wsl_bwrap / container 的 manager + router）接进执行道
# 之前，`jailed` 一律不发放：安装态可以是 `ready`，但没有后端就意味着没有隔离，
# 这时把会话放进 jailed 等于给它开数据面而没有人看守。
#
# P1a 的代码已经写完，是否发放改由 `G3KU_SANDBOX_ENABLE_JAIL` 控制：默认关，
# 只有在装好后端的机器上跑完验收清单（docs/architecture/sandbox-execution-plane.md
# 的「上机验收清单」）之后才允许打开。
JAIL_BACKENDS_AVAILABLE = False
JAIL_ENABLE_ENV = "G3KU_SANDBOX_ENABLE_JAIL"
_TRUTHY = {"1", "true", "yes", "on"}


def jail_backends_available() -> bool:
    if JAIL_BACKENDS_AVAILABLE:
        return True
    return str(os.getenv(JAIL_ENABLE_ENV, "")).strip().lower() in _TRUTHY

# 运行时基础工具：agent 循环与工具/技能 hydrated 的必需项。它们不是安全边界，
# 不允许在会话工具列表里取消；关闭 `silent` 或 loader 会让会话本身失效。
LIMITED_FIXED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "silent",
        "time",
        "load_tool_context",
        "load_tool_context_v2",
        "load_skill_context",
        "load_skill_context_v2",
    }
)

# 受限模式的默认工具集合。写类工具只允许在会话工作目录内落盘（由 fs 写根约束）；
# `web_fetch` 默认允许；`exec` / `process_kill` / `create_async_task` / `skill-installer`
# / `register_stickers` 默认不勾选，但 operator 可以在按会话工具列表里显式打开。
LIMITED_DEFAULT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # 读类数据面（受读根限定）
        "content_open",
        "content_search",
        "content_describe",
        "filesystem_stat",
        "view_message_history",
        # 写类数据面（受会话工作目录写根限定）
        "filesystem_write",
        "filesystem_edit",
        "filesystem_copy",
        "filesystem_move",
        "filesystem_delete",
        "filesystem_propose_patch",
        # 记忆、调度、渠道提醒
        "memory_update",
        "memory_note",
        "cron",
        "schedule_wake",
        "mention_member",
        # 网络默认允许（operator 可在工具列表取消）
        "web_fetch",
        # 拼图/索引选图：渲染贴纸缓存图并返回路径，不碰用户文件
        "show_sticker_category",
    }
)

# 旧名兼容：上一版第三方只读白名单的引用点继续可读；语义现在是“受限模式默认集合”。
READONLY_THIRD_PARTY_TOOL_NAMES = LIMITED_DEFAULT_TOOL_NAMES

# 勾选后确有额外后果、UI 需要给出说明的工具。
LIMITED_TOOL_WARNINGS: dict[str, str] = {
    "exec": "命令行执行会触达宿主；本档强制 governed（形态守卫 + 路径策略 + 命令行风险拦截）。",
    "process_kill": "可终止宿主进程；只应在本会话确实需要时打开。",
    "create_async_task": "任务在独立 worker 执行，不受本会话工作目录约束。",
    "skill-installer": "会安装工具到工作区，不受本会话工作目录约束。",
    "register_stickers": "会写全局表情包注册表，不受本会话工作目录约束。",
}

# register_stickers 会改名并写 registry.json，属于全局写入，默认不进受限集合。

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
# 调度类与派发类分开：`cron` / `schedule_wake` 在受限/沙盒档可用，任务派发仍拒。
SCHEDULE_TOOL_NAMES: frozenset[str] = frozenset({"cron", "schedule_wake"})
TASK_DISPATCH_TOOL_NAMES: frozenset[str] = frozenset({"create_async_task", "send_message"})
# 兼容旧引用：任务族 = 派发 + 调度。
TASK_TOOL_NAMES: frozenset[str] = TASK_DISPATCH_TOOL_NAMES | SCHEDULE_TOOL_NAMES

# owner 只读档（旧档）：写类 / 进程控制 / 任务族 / 会写盘的联网工具一律拒。
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


def is_task_dispatch_tool_name(tool_name: Any) -> bool:
    """派发族：`create_async_task` / `send_message` / `task_*`；不含调度类。"""
    name = str(tool_name or "").strip()
    if not name:
        return False
    return name in TASK_DISPATCH_TOOL_NAMES or name.startswith(_TASK_TOOL_PREFIX)


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
        jail_available = jail_backends_available()
    if not jail_available:
        # 后端没接进执行道：安装态再好看也只给受限档，绝不发放无人看守的数据面。
        return TIER_LIMITED
    install = str(install_state or "").strip().lower() or INSTALL_NOT_INSTALLED
    if install == INSTALL_READY:
        return TIER_JAILED
    # partial 只服务 owner：third_party 的必需维度（fs_read/fs_write/network）不全时
    # 绝不把它放进 jail——那会把"缺一维"当"全缺"或反过来，两种都是假安全。
    if install == INSTALL_PARTIAL and normalize_trust(trust) == TRUST_OWNER:
        return TIER_JAILED
    return TIER_LIMITED


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    session_key: str
    trust: str
    mode: str
    install_state: str
    tier: str
    # 受限档生效集合；None = 用默认集合。两项都只在 limited 档有意义。
    allowed_tool_names: frozenset[str] | None = None
    explicit_allowed_tool_names: frozenset[str] | None = None

    @property
    def restricted(self) -> bool:
        return self.tier != TIER_HOST_FULL

    @property
    def allowlist(self) -> frozenset[str] | None:
        """None = 不设白名单（宿主全权限 / 沙箱档 / owner 旧只读走黑名单）。"""
        if self.tier == TIER_LIMITED:
            return self.allowed_tool_names or (
                LIMITED_DEFAULT_TOOL_NAMES | LIMITED_FIXED_TOOL_NAMES
            )
        if self.tier == TIER_READONLY and self.trust != TRUST_OWNER:
            return READONLY_THIRD_PARTY_TOOL_NAMES
        return None

    @property
    def denied(self) -> frozenset[str]:
        if self.tier == TIER_READONLY and self.trust == TRUST_OWNER:
            return _OWNER_READONLY_DENIED
        if self.tier == TIER_JAILED:
            # 沙箱档数据面放开，但任务派发仍然不许：调度类（cron/schedule_wake）例外。
            return TASK_DISPATCH_TOOL_NAMES
        return frozenset()

    @property
    def exec_requires_governed(self) -> bool:
        """受限档的 exec 必须落到 governed（形态守卫 + 路径策略 + 工作区监禁）。"""
        if self.tier == TIER_LIMITED:
            return self.exec_allowed
        return self.tier == TIER_READONLY and self.trust == TRUST_OWNER

    @property
    def exec_allowed(self) -> bool:
        return self.tool_allowed("exec")

    @property
    def tool_policy_signature(self) -> str:
        """档位 + 生效工具集合的稳定签名；切档/改工具列表时变化。"""
        names = "\n".join(sorted(self.allowlist or ()))
        return f"{self.tier}\n{names}"

    @property
    def effective_tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.allowlist or ()))

    def tool_allowed(self, tool_name: Any) -> bool:
        name = str(tool_name or "").strip()
        if not name:
            return False
        # 任务派发族在受限/沙箱档一律拒；调度类不被这一条挡住。
        if is_task_dispatch_tool_name(name) and self.restricted:
            return False
        if name in self.denied:
            return False
        if self.tier == TIER_LIMITED:
            return name in (self.allowlist or frozenset())
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
    tier = resolve_tier(trust=trust, mode=normalized_mode, install_state=install)
    effective_tools: frozenset[str] | None = None
    explicit_tools: frozenset[str] | None = None
    if tier == TIER_LIMITED:
        stored = _stored_allowed_tools(raw_key)
        explicit_tools = frozenset(stored) if stored else None
        effective_tools = (explicit_tools or LIMITED_DEFAULT_TOOL_NAMES) | LIMITED_FIXED_TOOL_NAMES
    return SessionPolicy(
        session_key=raw_key,
        trust=trust,
        mode=normalized_mode,
        install_state=install,
        tier=tier,
        allowed_tool_names=effective_tools,
        explicit_allowed_tool_names=explicit_tools,
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


def _stored_allowed_tools(session_key: str) -> list[str]:
    try:
        from g3ku.sandbox.state import get_session_allowed_tools
    except Exception:  # noqa: BLE001
        return []
    try:
        return list(get_session_allowed_tools(session_key) or [])
    except Exception:  # noqa: BLE001
        return []


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
    "LIMITED_DEFAULT_TOOL_NAMES",
    "LIMITED_FIXED_TOOL_NAMES",
    "LIMITED_TOOL_WARNINGS",
    "MODE_RESTRICTED",
    "MODE_UNRESTRICTED",
    "NETWORK_TOOL_NAMES",
    "NETWORK_WRITE_TOOL_NAMES",
    "PROCESS_TOOL_NAMES",
    "READONLY_THIRD_PARTY_TOOL_NAMES",
    "SCHEDULE_TOOL_NAMES",
    "SessionPolicy",
    "TASK_DISPATCH_TOOL_NAMES",
    "TASK_TOOL_NAMES",
    "TIER_HOST_FULL",
    "TIER_JAILED",
    "TIER_LIMITED",
    "TIER_READONLY",
    "WRITE_TOOL_NAMES",
    "default_mode_for_trust",
    "is_task_dispatch_tool_name",
    "is_task_tool_name",
    "jail_backends_available",
    "resolve_tier",
    "session_policy",
]
