"""`keep_tools` / `keep_skills` 的提取与落账本（刀二）。

一条阶段被模型点名裁撤（`drop_completed_stage_tool_detail`）时，它的水合契约正文跟着
工具肉身一起离开上下文 —— 契约不在场，工具就不可调用（判据见
`g3ku/runtime/tool_context_presence.py`）。`keep_tools` / `keep_skills` 是模型唯一的
补救出口：**在提交点一次性**从资源文件重渲染正文、写进该阶段台账
（`kept_tool_contexts` / `kept_skill_contexts`），此后逐轮只回放账本。

为什么必须是提交点快照、不能逐轮重读文件：阶段块落在历史中段，逐轮重读会让运营者或
`skill-installer` 改一次 `toolskills/SKILL.md` 就改动块字节，块之后的整段前缀缓存全断。

提取失败（缺文件、资源改名、repair-required）时该条**不写**：工具保持撤销态，模型必须
重新 load —— 这是安全方向；回执里点名"未能保留：X（原因）"，绝不静默留一条空正文让模型
以为留住了。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from g3ku.runtime.tool_context_presence import (
    BODY_FIELD,
    FINGERPRINT_FIELD,
    LOADER_TOOL_NAMES,
    TOOL_ID_FIELD,
)

SKILL_LOADER_TOOL_NAMES = frozenset({"load_skill_context", "load_skill_context_v2"})

KEPT_TOOL_CONTEXTS_FIELD = "kept_tool_contexts"
KEPT_SKILL_CONTEXTS_FIELD = "kept_skill_contexts"
SKILL_ID_FIELD = "skill_id"

# 失败原因码：回执里逐条点名，模型据此决定要不要重新 load。
FAILURE_TOOL_NOT_FOUND = "tool_resource_not_found"
FAILURE_TOOL_BODY_EMPTY = "tool_contract_body_empty"
FAILURE_TOOL_REPAIR_REQUIRED = "tool_repair_required"
FAILURE_SKILL_NOT_FOUND = "skill_resource_not_found"
FAILURE_SKILL_BODY_EMPTY = "skill_body_empty"
# 提交落点收到了名字、却没收到任何对应的快照交代（绕过工具层的写入者，如恢复重放）。
# 这一条必须存在，否则"名字被接受 + 账本里没正文"就是模型读不到的空承诺。
FAILURE_NOT_RESOLVED = "keep_not_resolved_at_submit"

_EMPTY_FAMILY_REGISTRY: Any = None

# 没裁撤时点名保留是一句空承诺：正文没有阶段块可以放。两条车道的闸门都会按参数错误拒掉
# 这一路（`keep_contracts_require_drop_error`），这份文案只服务绕过工具层的写入者。
KEEP_CONTRACT_NOT_DROPPED_NOTE = (
    "keep_tools / keep_skills 未生效：这次没有点名 drop_completed_stage_tool_detail，"
    "没有阶段块可以放保留正文，所点名的契约一条都没留下"
)

# 失败原因码 → 回执里给模型读的那句原因。只给码等于没给：模型分不清"改名了"和"这条本来
# 就是空的"，也就不知道自己该不该重新 load。
KEEP_CONTRACT_FAILURE_REASON_TEXT = {
    FAILURE_TOOL_NOT_FOUND: "工具契约取不到（资源改名或已删除），它仍是撤销态，要用的话得重新 load_tool_context",
    FAILURE_TOOL_BODY_EMPTY: "工具契约正文为空，留不下任何东西",
    FAILURE_TOOL_REPAIR_REQUIRED: "工具处于 repair-required 状态，正文不可用",
    FAILURE_SKILL_NOT_FOUND: "技能正文取不到（资源改名或已删除）",
    FAILURE_SKILL_BODY_EMPTY: "技能正文为空，留不下任何东西",
    FAILURE_NOT_RESOLVED: "这次的提交落点没有做保留快照，正文一条都没写下，要用的话得重新 load",
}


def _empty_family_registry() -> Any:
    """给 `build_tool_toolskill_payload` 的空家族视图：没有治理库时按单体描述符渲染。

    注入式 `tool_payload_getter`（节点道由 `MainRuntimeService.get_tool_toolskill` 提供）
    走的是家族解析那条路，指纹与 `load_tool_context` 逐字一致；这条回退只在拿不到服务时
    兜底，同一份正文、同一套指纹输入，缺的只是家族级 warnings 与 exec 策略。
    """
    global _EMPTY_FAMILY_REGISTRY
    if _EMPTY_FAMILY_REGISTRY is None:
        from types import SimpleNamespace

        _EMPTY_FAMILY_REGISTRY = SimpleNamespace(list_tool_families=lambda: [])
    return _EMPTY_FAMILY_REGISTRY


def _field(record: Any, key: str, default: Any = None) -> Any:
    """账本条目既可能是 dict（前门）也可能是模型对象（节点），两种读法同价。"""
    if isinstance(record, dict):
        return record.get(key, default)
    return getattr(record, key, default)


def _loader_arguments(record: Any) -> dict[str, Any]:
    """从一条轮次记录里取出加载器的入参字典。

    前门的轮次内联 `arguments`（dict）与 `arguments_text`；节点账本只按 call id 关联
    `task_node_tool_results`，那里只有 `arguments_text`。两条都得支持，否则节点道的
    `keep_tools` 永远收不到名字。
    """
    arguments = _field(record, "arguments")
    if isinstance(arguments, dict) and arguments:
        return arguments
    text = str(_field(record, "arguments_text") or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _loader_record_succeeded(record: Any) -> bool:
    status = str(_field(record, "status") or "").strip().lower()
    if not status:
        return True
    return status in {"success", "ok", "succeeded"}


def collect_stage_loader_names(
    stage: Any,
    *,
    tool_results_by_call: dict[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
    """这条阶段真正 load 过的工具与技能名（按出现顺序、去重）。

    `keep_tools` / `keep_skills` 的名字必须来自这份集合 —— 它是"本阶段loader 记录"的
    唯一口径，也是未知名字被拒时回执里枚举的那一份名单。
    """
    results_by_call = tool_results_by_call or {}
    tool_names: list[str] = []
    skill_names: list[str] = []
    seen_tools: set[str] = set()
    seen_skills: set[str] = set()
    for round_item in list(_field(stage, "rounds", []) or []):
        records: list[Any] = [dict(item) for item in list(_field(round_item, "tools", []) or []) if isinstance(item, dict)]
        for call_id in list(_field(round_item, "tool_call_ids", []) or []):
            row = results_by_call.get(str(call_id or "").strip())
            if isinstance(row, dict):
                records.append(row)
        for record in records:
            loader_name = str(_field(record, "tool_name") or _field(record, "name") or "").strip()
            if loader_name not in LOADER_TOOL_NAMES | SKILL_LOADER_TOOL_NAMES:
                continue
            if not _loader_record_succeeded(record):
                continue
            arguments = _loader_arguments(record)
            if loader_name in LOADER_TOOL_NAMES:
                candidate = str(arguments.get(TOOL_ID_FIELD) or "").strip()
                if candidate and candidate not in seen_tools:
                    seen_tools.add(candidate)
                    tool_names.append(candidate)
                continue
            candidate = str(arguments.get(SKILL_ID_FIELD) or "").strip()
            if candidate and candidate not in seen_skills:
                seen_skills.add(candidate)
                skill_names.append(candidate)
    return tool_names, skill_names


def tool_results_by_call_from_rows(rows: Iterable[Any] | None) -> dict[str, dict[str, Any]]:
    """把 `task_node_tool_results` 的行按 call id 索引，只留判据要用的三列。"""
    collected: dict[str, dict[str, Any]] = {}
    for row in list(rows or []):
        call_id = str(_field(row, "tool_call_id") or "").strip()
        if not call_id:
            continue
        collected[call_id] = {
            "tool_name": str(_field(row, "tool_name") or ""),
            "status": str(_field(row, "status") or ""),
            "arguments_text": str(_field(row, "arguments_text") or ""),
        }
    return collected


def _fallback_tool_payload_getter(resource_manager: Any) -> Callable[[str], dict[str, Any] | None]:
    def _get_payload(tool_id: str) -> dict[str, Any] | None:
        if resource_manager is None:
            return None
        descriptor = resource_manager.get_tool_descriptor(tool_id)
        if descriptor is None:
            return None
        from main.governance.tool_context import build_tool_toolskill_payload

        return build_tool_toolskill_payload(
            tool_id,
            raw_tool_family_getter=lambda _name: None,
            resource_registry=_empty_family_registry(),
            resource_manager=resource_manager,
        )

    return _get_payload


def _default_skill_body_getter(workspace_root: Path | None, resource_manager: Any) -> Callable[[str], str | None]:
    def _get_body(skill_id: str) -> str | None:
        from g3ku.agent.skills import SkillsLoader

        loader = SkillsLoader(Path(workspace_root or Path.cwd()), resource_manager=resource_manager)
        return loader.load_skill(skill_id)

    return _get_body


def render_kept_tool_context(
    tool_id: str,
    *,
    tool_payload_getter: Callable[[str], dict[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], str]:
    """渲染一条保留契约。返回 ``(条目或空字典, 失败原因码)``。

    指纹按**当前内容**重算（`build_tool_context_fingerprint` 的输入键集合与
    `load_tool_context` 那条完全相同），所以块内正文与重新 load 出来的正文同指纹时，
    重复读守卫认得出"这一跳已经在场"。
    """
    normalized_tool_id = str(tool_id or "").strip()
    if not normalized_tool_id or tool_payload_getter is None:
        return {}, FAILURE_TOOL_NOT_FOUND
    try:
        payload = tool_payload_getter(normalized_tool_id)
    except Exception as exc:
        return {}, f"{FAILURE_TOOL_NOT_FOUND}:{type(exc).__name__}"
    if not isinstance(payload, dict) or not payload:
        return {}, FAILURE_TOOL_NOT_FOUND
    if bool(payload.get("repair_required")):
        return {}, FAILURE_TOOL_REPAIR_REQUIRED
    body = str(payload.get("content") or "")
    if not body.strip():
        return {}, FAILURE_TOOL_BODY_EMPTY
    from main.governance.tool_context import build_tool_context_fingerprint

    fingerprint = str(payload.get(FINGERPRINT_FIELD) or "").strip() or build_tool_context_fingerprint(payload)
    if not fingerprint:
        return {}, FAILURE_TOOL_BODY_EMPTY
    return (
        {
            TOOL_ID_FIELD: str(payload.get(TOOL_ID_FIELD) or "").strip() or normalized_tool_id,
            FINGERPRINT_FIELD: fingerprint,
            BODY_FIELD: body,
        },
        "",
    )


def render_kept_skill_context(
    skill_id: str,
    *,
    skill_body_getter: Callable[[str], str | None] | None = None,
) -> tuple[dict[str, Any], str]:
    normalized_skill_id = str(skill_id or "").strip()
    if not normalized_skill_id or skill_body_getter is None:
        return {}, FAILURE_SKILL_NOT_FOUND
    try:
        body = skill_body_getter(normalized_skill_id)
    except Exception as exc:
        return {}, f"{FAILURE_SKILL_NOT_FOUND}:{type(exc).__name__}"
    if body is None:
        return {}, FAILURE_SKILL_NOT_FOUND
    text = str(body)
    if not text.strip():
        return {}, FAILURE_SKILL_BODY_EMPTY
    return ({SKILL_ID_FIELD: normalized_skill_id, BODY_FIELD: text}, "")


def build_kept_contract_snapshot(
    *,
    tool_ids: Iterable[str] | None = None,
    skill_ids: Iterable[str] | None = None,
    tool_payload_getter: Callable[[str], dict[str, Any] | None] | None = None,
    skill_body_getter: Callable[[str], str | None] | None = None,
    resource_manager: Any = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """一次性快照：把点名的契约正文取出来，取不到的点名回报。

    两个车道的提交落点都调它，差别只在传不传 `tool_payload_getter`：节点道由
    `MainRuntimeService.get_tool_toolskill` 注入（与 `load_tool_context` 同一份渲染），
    前门同理；拿不到服务时回落到共享资源管理器，正文一致、家族级附加项可能缺。
    """
    effective_tool_payload_getter = tool_payload_getter or _fallback_tool_payload_getter(resource_manager)
    effective_skill_body_getter = skill_body_getter or _default_skill_body_getter(workspace_root, resource_manager)

    tool_contexts: list[dict[str, Any]] = []
    skill_contexts: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    seen_tools: set[str] = set()
    seen_skills: set[str] = set()

    for raw_tool_id in list(tool_ids or []):
        tool_id = str(raw_tool_id or "").strip()
        if not tool_id or tool_id in seen_tools:
            continue
        seen_tools.add(tool_id)
        entry, reason = render_kept_tool_context(tool_id, tool_payload_getter=effective_tool_payload_getter)
        if reason:
            failures.append({"kind": "tool", "name": tool_id, "reason": reason})
            continue
        tool_contexts.append(entry)

    for raw_skill_id in list(skill_ids or []):
        skill_id = str(raw_skill_id or "").strip()
        if not skill_id or skill_id in seen_skills:
            continue
        seen_skills.add(skill_id)
        entry, reason = render_kept_skill_context(skill_id, skill_body_getter=effective_skill_body_getter)
        if reason:
            failures.append({"kind": "skill", "name": skill_id, "reason": reason})
            continue
        skill_contexts.append(entry)

    return {
        "tool_contexts": tool_contexts,
        "skill_contexts": skill_contexts,
        "failures": failures,
    }


def normalize_kept_skill_contexts(raw: Any) -> list[dict[str, Any]]:
    """保留技能正文的归一化白名单：只留 ``skill_id`` / 正文。

    与 `normalize_kept_tool_contexts` 同一类落点：漏一份白名单就等于逐轮被抹掉。技能不
    进水合台账，所以这份列表只服务渲染，不参与在场判据。
    """
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in list(raw or []):
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get(SKILL_ID_FIELD) or "").strip()
        if not skill_id or skill_id in seen:
            continue
        seen.add(skill_id)
        normalized.append({SKILL_ID_FIELD: skill_id, BODY_FIELD: str(item.get(BODY_FIELD) or "")})
    return normalized


def _kept_names(entries: Any, key: str) -> list[str]:
    collected: list[str] = []
    for item in list(entries or []):
        if not isinstance(item, dict):
            continue
        name = str(item.get(key) or "").strip()
        if name and name not in collected:
            collected.append(name)
    return collected


def _failure_reason_text(reason: Any) -> str:
    """原因码 → 给模型读的一句话；`code:ExcName` 这种带后缀的码把后缀一并点名。"""
    text = str(reason or "").strip()
    code, _sep, suffix = text.partition(":")
    base = KEEP_CONTRACT_FAILURE_REASON_TEXT.get(code) or "提取失败"
    return f"{base}（{suffix}）" if suffix else base


def _dedup_names(values: Any) -> list[str]:
    collected: list[str] = []
    for raw in list(values or []):
        name = str(raw or "").strip()
        if name and name not in collected:
            collected.append(name)
    return collected


def complete_keep_snapshot(
    snapshot: Any,
    *,
    keep_tools: Any = None,
    keep_skills: Any = None,
) -> dict[str, Any]:
    """把提交落点收到的快照与模型点名的名字**对账**：没被交代过的补一条失败。

    取一条契约只有两种结局——写了正文，或点名了失败原因。第三条路（名字收下、账本里既没
    正文也没有原因）是模型读不到的空承诺，而它恰恰是绕过工具层的写入者最容易走进去的那条。
    """
    payload = dict(snapshot) if isinstance(snapshot, dict) else {}
    tool_contexts = [item for item in list(payload.get("tool_contexts") or []) if isinstance(item, dict)]
    skill_contexts = [item for item in list(payload.get("skill_contexts") or []) if isinstance(item, dict)]
    failures = [item for item in list(payload.get("failures") or []) if isinstance(item, dict)]
    accounted_tools = {str(item.get(TOOL_ID_FIELD) or "").strip() for item in tool_contexts}
    accounted_skills = {str(item.get(SKILL_ID_FIELD) or "").strip() for item in skill_contexts}
    failures = [dict(item) for item in failures]
    for item in failures:
        if str(item.get("kind") or "").strip() == "tool":
            accounted_tools.add(str(item.get("name") or "").strip())
        else:
            accounted_skills.add(str(item.get("name") or "").strip())
    for name in _dedup_names(keep_tools):
        if name not in accounted_tools:
            accounted_tools.add(name)
            failures.append({"kind": "tool", "name": name, "reason": FAILURE_NOT_RESOLVED})
    for name in _dedup_names(keep_skills):
        if name not in accounted_skills:
            accounted_skills.add(name)
            failures.append({"kind": "skill", "name": name, "reason": FAILURE_NOT_RESOLVED})
    payload["tool_contexts"] = tool_contexts
    payload["skill_contexts"] = skill_contexts
    payload["failures"] = failures
    return payload


def keep_closure_fields(snapshot: Any) -> dict[str, Any]:
    """`stage_closure` 回执里保留契约那部分，两车道共用同一份形状。

    留下的是**名字**，不是"你以为你留住了"：取不到的那几条逐条点名原因（`keep_failed` 给
    机器码 + 可读句，`note` 给一句人话），模型才分得清该不该重新 load。什么都没点名时返回
    空字典，不给没用到这功能的提交添一行噪声。
    """
    payload = dict(snapshot or {}) if isinstance(snapshot, dict) else {}
    kept_tools = _kept_names(payload.get("tool_contexts"), TOOL_ID_FIELD)
    kept_skills = _kept_names(payload.get("skill_contexts"), SKILL_ID_FIELD)
    failures = [
        {
            "kind": str(item.get("kind") or "").strip(),
            "name": str(item.get("name") or "").strip(),
            "reason": str(item.get("reason") or "").strip(),
            "detail": _failure_reason_text(item.get("reason")),
        }
        for item in list(payload.get("failures") or [])
        if isinstance(item, dict)
    ]
    note = str(payload.get("note") or "").strip()
    if not kept_tools and not kept_skills and not failures and not note:
        return {}
    if failures:
        named = "未能保留：" + "、".join(f"{item.get('name')}（{item.get('detail')}）" for item in failures)
        note = f"{note}；{named}" if note else named
    fields: dict[str, Any] = {
        "kept_tools": kept_tools,
        "kept_skills": kept_skills,
        "keep_failed": failures,
    }
    if note:
        fields["note"] = note
    return fields


def keep_contract_name_error(field: str, unknown: list[str], allowed: list[str]) -> str:
    """未知保留名的拒绝文案：点名错在哪，并枚举这条判据自己依据的那份名单。

    只说"名字不合法"等于让模型再猜一轮：它不知道本阶段到底 load 过什么。枚举的是**判据
    实际使用的集合**（被移除阶段的 loader 记录），不是全量可见集——后者会把模型引向
    " load 别的工具再保留"的死路。
    """
    names = ", ".join([str(item or "").strip() for item in list(unknown or []) if str(item or "").strip()])
    if allowed:
        allowed_text = f"{field} may only name what this closing stage actually loaded: " + ", ".join(
            [str(item or "").strip() for item in list(allowed or []) if str(item or "").strip()]
        )
    else:
        allowed_text = (
            f"{field} may only name what this closing stage actually loaded, "
            "and this stage has no such loader record - nothing can be kept"
        )
    return f"{field}: {names} was not loaded during the stage you are closing. {allowed_text}"


def resolve_kept_contracts(
    stage: Any,
    *,
    tool_results_by_call: dict[str, Any] | None = None,
    keep_tools: list[str] | None = None,
    keep_skills: list[str] | None = None,
    tool_payload_getter: Callable[[str], dict[str, Any] | None] | None = None,
    skill_body_getter: Callable[[str], str | None] | None = None,
    resource_manager: Any = None,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    """一条阶段的 `keep_*` 收口：校验名字 → 一次性取正文 → 带回执字段。

    两条车道的提交落点共用这份判据，拒绝口径与回执形状因此只有一处定义。名字不在本阶段
    loader 记录里时整批拒绝（不做"保留能认的那几条"）：半收半拒会让模型读到与它提交内容
    不一致的结果。
    """
    loaded_tool_names, loaded_skill_names = collect_stage_loader_names(
        stage,
        tool_results_by_call=tool_results_by_call,
    )
    requested_tools = list(keep_tools or [])
    requested_skills = list(keep_skills or [])
    unknown_tools = [name for name in requested_tools if name not in set(loaded_tool_names)]
    unknown_skills = [name for name in requested_skills if name not in set(loaded_skill_names)]
    if unknown_tools:
        raise ValueError(keep_contract_name_error("keep_tools", unknown_tools, loaded_tool_names))
    if unknown_skills:
        raise ValueError(keep_contract_name_error("keep_skills", unknown_skills, loaded_skill_names))
    if not requested_tools and not requested_skills:
        return {
            "tool_contexts": [],
            "skill_contexts": [],
            "failures": [],
            "loaded_tool_names": loaded_tool_names,
            "loaded_skill_names": loaded_skill_names,
        }
    snapshot = build_kept_contract_snapshot(
        tool_ids=requested_tools,
        skill_ids=requested_skills,
        tool_payload_getter=tool_payload_getter,
        skill_body_getter=skill_body_getter,
        resource_manager=resource_manager,
        workspace_root=workspace_root,
    )
    snapshot["loaded_tool_names"] = loaded_tool_names
    snapshot["loaded_skill_names"] = loaded_skill_names
    return snapshot


__all__ = [
    "KEEP_CONTRACT_NOT_DROPPED_NOTE",
    "KEPT_SKILL_CONTEXTS_FIELD",
    "KEPT_TOOL_CONTEXTS_FIELD",
    "SKILL_ID_FIELD",
    "SKILL_LOADER_TOOL_NAMES",
    "build_kept_contract_snapshot",
    "collect_stage_loader_names",
    "complete_keep_snapshot",
    "keep_closure_fields",
    "keep_contract_name_error",
    "normalize_kept_skill_contexts",
    "render_kept_skill_context",
    "render_kept_tool_context",
    "resolve_kept_contracts",
    "tool_results_by_call_from_rows",
]
