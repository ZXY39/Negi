"""`reuse_args` 的入参暂存（stash）。

一句话语义：模型直接调用一个还没水合的工具时，**这一跳不执行**，入参校验通过后被暂存；
下一跳它对该工具填 `reuse_args=true`，就把这批暂存的全部入参一次跑完。

为什么必须耐久：`reuse_args` 是布尔、模型看不见 handle，pause/resume 或重启后如果暂存
没了，它无法自己推断原因，只会重复失败的调用。所以两条车道都落在耐久载体上：

- 节点 → `node.metadata[\"tool_arg_stash\"]`（整份 metadata 是 JSON 落库，不在 runtime
  frame 的白名单里——帧白名单不收这个键，去帧里读会永远读出空）。
- 前门 → 会话连续性侧车 `tool_arg_stash`（与 `skill_roster_notice_context` 同一条路：
  **必须同时登记 `web_ceo_sessions` 的装配白名单**，只写不登记等于静默不落盘）。

本模块只做**容器算术**（纯函数，进出都是那份 dict），不碰存储：谁持有载体、怎么落盘，
由两条车道各自决定。这样"严格一跳""同轮保序""同签名折叠"这三条规则能被单独测。

入参只在校验通过后入栈（D3），所以这里的条目都是可以直接执行的形状；权限不在这里判——
`reuse_args` 只是传输优化，不构成授权，执行前还要重跑 RBAC / repair-required（D7）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

STASH_CONTAINER_KEY = "tool_arg_stash"

ARGS_FIELD = "args"
STAGED_AT_FIELD = "staged_at"
STAGED_HOP_FIELD = "staged_hop"
FINGERPRINT_FIELD = "tool_context_fingerprint"
DIGEST_FIELD = "args_digest"

NODE_SCOPE_PREFIX = "node"
SESSION_SCOPE_PREFIX = "session"


def node_scope_key(task_id: str, node_id: str, tool_id: str) -> str:
    return f"{NODE_SCOPE_PREFIX}:{str(task_id or '').strip()}:{str(node_id or '').strip()}:{str(tool_id or '').strip()}"


def session_scope_key(session_key: str, tool_id: str) -> str:
    return f"{SESSION_SCOPE_PREFIX}:{str(session_key or '').strip()}:{str(tool_id or '').strip()}"


def scope_tool_id(scope_key: str) -> str:
    """取 scope 末段的工具名。中间段可能自带冒号（会话 key 就是 `web:shared`），
    所以这里只按**末段**取，也正因为如此不能靠数段数来判形状。"""
    return str(scope_key or "").strip().rsplit(":", 1)[-1]


def normalize_staged_args(args: Any) -> dict[str, Any]:
    """暂存前归一化成 dict：与执行侧同源的那份归一化入参（键序无关）。"""
    if isinstance(args, str):
        text = args.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                return dict(parsed)
        return {"value": args}
    if isinstance(args, dict):
        return dict(args)
    if args is None:
        return {}
    return {"value": args}


def args_digest(args: Any) -> str:
    """同签名判据：与跨轮熔断同一口径（排序键、UTF-8）。"""
    normalized = normalize_staged_args(args)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return f"arg:{hashlib.sha256(encoded).hexdigest()[:16]}"


def normalize_tool_arg_stash(payload: Any) -> dict[str, Any]:
    """持久化入口的形状闸门：两条车道的载体都先过这里再落盘。

    与 `read_container` 的区别是它接受**任意**落盘形态（旧版本残留、被截断的 JSON、别的
    键），并总是产出一份可安全写回的容器；漏掉这一步等于让脏数据进到执行侧。
    """
    if not isinstance(payload, dict):
        return {}
    container = payload.get(STASH_CONTAINER_KEY) if STASH_CONTAINER_KEY in payload else payload
    if not isinstance(container, dict):
        return {}
    normalized: dict[str, list[dict[str, Any]]] = {}
    for scope_key, entries in container.items():
        scope = str(scope_key or "").strip()
        if not scope or not scope_key_has_shape(scope):
            continue
        if not isinstance(entries, list):
            continue
        rows: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            args = normalize_staged_args(entry.get(ARGS_FIELD))
            rows.append(
                {
                    ARGS_FIELD: args,
                    DIGEST_FIELD: str(entry.get(DIGEST_FIELD) or "").strip() or args_digest(args),
                    FINGERPRINT_FIELD: str(entry.get(FINGERPRINT_FIELD) or "").strip(),
                    STAGED_AT_FIELD: str(entry.get(STAGED_AT_FIELD) or "").strip(),
                    STAGED_HOP_FIELD: int(entry.get(STAGED_HOP_FIELD) or 0),
                }
            )
        if rows:
            normalized[scope] = rows
    return normalized


def scope_key_has_shape(scope_key: str) -> bool:
    """scope 必须带已知前缀、末段是工具名、且前缀与末段之间至少还有一段。

    不数段数：会话 key 自己就含冒号（`web:shared`），按段数判会把合法 scope 判成畸形，
    于是暂存被静默丢弃、模型下一跳的 `reuse_args` 读成"没有可复用入参"。这条判据只用来
    挡住别的业务的键——判据是前缀 + 两端非空。
    """
    parts = str(scope_key or "").strip().split(":")
    if len(parts) < 3:
        return False
    prefix, middle, tool_id = parts[0], parts[1], parts[-1]
    if prefix not in (NODE_SCOPE_PREFIX, SESSION_SCOPE_PREFIX):
        return False
    return bool(middle.strip()) and bool(tool_id.strip())


def read_container(carrier: Any) -> dict[str, list[dict[str, Any]]]:
    """把载体里的容器取成可写副本；形状不对一律当空（不抛，读路径不该因脏数据停摆）。"""
    if not isinstance(carrier, dict):
        return {}
    raw = carrier.get(STASH_CONTAINER_KEY)
    if not isinstance(raw, dict):
        return {}
    container: dict[str, list[dict[str, Any]]] = {}
    for scope_key, entries in raw.items():
        scope = str(scope_key or "").strip()
        if not scope or not isinstance(entries, list):
            continue
        collected: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            collected.append(dict(entry))
        if collected:
            container[scope] = collected
    return container


def write_container(carrier: Any, container: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """写回容器：空列表的 scope 直接摘掉，不留 `"x": []` 这种噪声字节。"""
    next_carrier = dict(carrier) if isinstance(carrier, dict) else {}
    pruned = {str(scope): list(entries) for scope, entries in (container or {}).items() if str(scope or "").strip() and list(entries or [])}
    if pruned:
        next_carrier[STASH_CONTAINER_KEY] = pruned
    else:
        next_carrier.pop(STASH_CONTAINER_KEY, None)
    return next_carrier


def stage_args(
    container: dict[str, list[dict[str, Any]]],
    *,
    scope_key: str,
    args: Any,
    fingerprint: str,
    staged_at: str = "",
    staged_hop: int = 0,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], int]:
    """追加一份入参，返回 (容器, 新条目, 该 scope 现有份数)。

    **同轮同签名的重复调用由上游的同轮去重先折叠**（D5），这里只负责按调用顺序追加——
    不同入参一律全部保留，不做 last-write-wins。
    """
    next_container = {scope: list(entries) for scope, entries in (container or {}).items()}
    entry = {
        ARGS_FIELD: normalize_staged_args(args),
        DIGEST_FIELD: args_digest(args),
        FINGERPRINT_FIELD: str(fingerprint or "").strip(),
        STAGED_AT_FIELD: str(staged_at or "").strip(),
        STAGED_HOP_FIELD: int(staged_hop or 0),
    }
    next_container.setdefault(scope_key, []).append(entry)
    return next_container, entry, len(next_container[scope_key])


def peek_entries(
    container: dict[str, list[dict[str, Any]]],
    *,
    scope_key: str,
    current_hop: int = 0,
    fingerprint: str = "",
) -> list[dict[str, Any]]:
    """当前跳可消费的条目：严格一跳，修订不同步的条目也不给（D7 前置由调用方判契约在场）。"""
    entries = list((container or {}).get(scope_key) or [])
    usable: list[dict[str, Any]] = []
    for entry in entries:
        staged_hop = int(entry.get(STAGED_HOP_FIELD) or 0)
        if staged_hop != int(current_hop or 0) - 1:
            continue
        if fingerprint and str(entry.get(FINGERPRINT_FIELD) or "").strip() != str(fingerprint or "").strip():
            continue
        usable.append(entry)
    return usable


def consume_entries(
    container: dict[str, list[dict[str, Any]]],
    *,
    scope_key: str,
    current_hop: int = 0,
    fingerprint: str = "",
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """一次取走该 scope 的**全部**可消费条目（不支持选择性消费，D7）。"""
    usable = peek_entries(container, scope_key=scope_key, current_hop=current_hop, fingerprint=fingerprint)
    next_container = {scope: list(entries) for scope, entries in (container or {}).items()}
    if usable:
        consumed_digests = {str(entry.get(DIGEST_FIELD) or "") for entry in usable}
        remaining = [
            entry
            for entry in next_container.get(scope_key, [])
            if str(entry.get(DIGEST_FIELD) or "") not in consumed_digests
        ]
        if remaining:
            next_container[scope_key] = remaining
        else:
            next_container.pop(scope_key, None)
    return next_container, usable


def drop_scope(
    container: dict[str, list[dict[str, Any]]],
    *,
    scope_key: str = "",
    tool_id: str = "",
) -> dict[str, list[dict[str, Any]]]:
    """按 scope 或按工具名摘掉条目（阶段切换、节点终态、契约离场都走这里）。"""
    next_container = {scope: list(entries) for scope, entries in (container or {}).items()}
    if scope_key:
        next_container.pop(scope_key, None)
        return next_container
    if tool_id:
        for scope in list(next_container):
            if scope_tool_id(scope) == str(tool_id or "").strip():
                next_container.pop(scope, None)
    return next_container


def sweep_expired(
    container: dict[str, list[dict[str, Any]]],
    *,
    current_hop: int = 0,
    present_tool_ids: set[str] | None = None,
    fingerprint_by_tool: dict[str, str] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """清掉"过了严格一跳"或"契约已离场"的条目，返回 (容器, 被清的 scope)。

    `present_tool_ids=None` 表示调用方这一跳没有在场名单可读——不按未知清空（撤销是收紧，
    读不到判据时不清，与契约在场判据同一条保守方向）。
    """
    next_container = {scope: list(entries) for scope, entries in (container or {}).items()}
    dropped: list[str] = []
    for scope in list(next_container):
        entries = next_container.get(scope) or []
        if not entries:
            dropped.append(scope)
            next_container.pop(scope, None)
            continue
        still_fresh = [
            entry
            for entry in entries
            if int(entry.get(STAGED_HOP_FIELD) or 0) >= int(current_hop or 0) - 1
        ]
        tool_id = scope_tool_id(scope)
        if present_tool_ids is not None and tool_id not in set(present_tool_ids):
            still_fresh = []
        elif fingerprint_by_tool and tool_id in set(fingerprint_by_tool):
            current_revision = str(fingerprint_by_tool.get(tool_id) or "").strip()
            if current_revision:
                still_fresh = [
                    entry
                    for entry in still_fresh
                    if str(entry.get(FINGERPRINT_FIELD) or "").strip() == current_revision
                ]
        if still_fresh:
            next_container[scope] = still_fresh
        else:
            dropped.append(scope)
            next_container.pop(scope, None)
    return next_container, dropped


def drop_staged_after(
    container: dict[str, list[dict[str, Any]]],
    *,
    cutoff_iso: str,
) -> dict[str, list[dict[str, Any]]]:
    """历史被回截（编辑/Fork）时，丢掉截断点**之后**暂存的入参。

    与 roster 台账同一条纪律，方向相反：那边是"只清历史不清台账＝名字永久忘掉"，这边是
    "只清历史不清暂存＝一段已经被抹掉的将来留下可执行入参"。条目按落盘时间比，比不了
    （缺时间戳）一律丢——宁可模型重填，不可按看不见的时间执行。
    """
    cutoff = str(cutoff_iso or "").strip()
    if not cutoff:
        return {}
    next_container: dict[str, list[dict[str, Any]]] = {}
    for scope, entries in (container or {}).items():
        kept = [
            dict(entry)
            for entry in list(entries or [])
            if str(entry.get(STAGED_AT_FIELD) or "").strip()
            and str(entry.get(STAGED_AT_FIELD) or "").strip() <= cutoff
        ]
        if kept:
            next_container[scope] = kept
    return next_container


def staged_targets(container: dict[str, list[dict[str, Any]]], *, scope_key: str) -> list[dict[str, Any]]:
    """回执要枚举的目标列表（D16：不可逆工具必须列全，不截断）。"""
    return [dict(entry) for entry in list((container or {}).get(scope_key) or [])]


def staged_count(container: dict[str, list[dict[str, Any]]], *, scope_key: str) -> int:
    return len(list((container or {}).get(scope_key) or []))


__all__ = [
    "ARGS_FIELD",
    "DIGEST_FIELD",
    "FINGERPRINT_FIELD",
    "STAGED_AT_FIELD",
    "STAGED_HOP_FIELD",
    "STASH_CONTAINER_KEY",
    "args_digest",
    "consume_entries",
    "drop_scope",
    "drop_staged_after",
    "node_scope_key",
    "normalize_staged_args",
    "normalize_tool_arg_stash",
    "peek_entries",
    "read_container",
    "session_scope_key",
    "scope_key_has_shape",
    "scope_tool_id",
    "stage_args",
    "staged_count",
    "staged_targets",
    "sweep_expired",
    "write_container",
]
