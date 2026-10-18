from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from g3ku.json_schema_utils import (
    build_example_from_schema,
    normalize_object_json_schema,
    render_parameter_contract_markdown,
)
from main.governance.exec_tool_policy import (
    EXEC_TOOL_ID,
    resolve_exec_runtime_policy_payload,
)

# 模型读到的 ok:true 必须自带"这次加载到底会不会提升"。此前 payload 只有
# callable_now（治理层"对该角色可调用"），本轮真不真的能调要另看曝光集，两者被混成
# 一件事，模型于是反复拿到"说它可用、调它报错"的循环。
PROMOTION_HELP = {
    "promoted_next_turn": "已记录水合，下一轮可直接调用；本轮仍不可调用。",
    "already_callable": "本轮已可直接调用，无需再加载本说明。",
    "already_hydrated": "该工具已在水合台账内，但不在本轮可调用名单里；请改用本轮可调用名单中的工具。",
    "not_in_this_turn_candidates": (
        "该名字不在本轮候选工具里，本次加载只返回说明、不会提升它；"
        "请从合同里的本轮候选工具名单中选择，或直接使用本轮可调用名单中的工具。"
    ),
    "not_promotable": "该工具不需要水合（常驻内置工具本轮直接可调用），请改用本轮可调用名单中的工具。",
}


def _normalized_runtime_name_list(values: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_value in list(values or []):
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def build_tool_context_fingerprint(payload: dict[str, Any] | None) -> str:
    normalized = dict(payload or {}) if isinstance(payload, dict) else {}
    if not normalized:
        return ""
    fingerprint_payload = {
        "tool_id": str(normalized.get("tool_id") or "").strip(),
        "content": str(normalized.get("content") or ""),
        "parameter_contract_markdown": str(normalized.get("parameter_contract_markdown") or ""),
        "required_parameters": [
            str(item or "").strip()
            for item in list(normalized.get("required_parameters") or [])
            if str(item or "").strip()
        ],
        "example_arguments": normalized.get("example_arguments") or {},
        "warnings": [
            str(item or "").strip()
            for item in list(normalized.get("warnings") or [])
            if str(item or "").strip()
        ],
        "errors": [
            str(item or "").strip()
            for item in list(normalized.get("errors") or [])
            if str(item or "").strip()
        ],
        "exec_runtime_policy": (
            dict(normalized.get("exec_runtime_policy") or {})
            if isinstance(normalized.get("exec_runtime_policy"), dict)
            else None
        ),
        # 刻意不含 callable / available / repair_required / callable_now /
        # will_be_hydrated_next_turn / hydration_targets：那几项是"本轮视角"，逐轮会变。
        # 指纹的身份是契约正文，重复读守卫拿它跨轮比对，掺进轮次状态后同一份正文会算出
        # 两个指纹，守卫就再也拦不住重读（实盘：同一工具同长度正文出现 2-4 个指纹）。
    }
    encoded = json.dumps(
        fingerprint_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"tcf:{hashlib.sha256(encoded).hexdigest()[:16]}"


def apply_runtime_tool_context_projection(
    payload: dict[str, Any] | None,
    *,
    requested_tool_id: str = "",
    runtime: dict[str, Any] | None = None,
    live_visible_tool_names: list[str] | None = None,
) -> dict[str, Any]:
    """把"本轮视角"字段（promotion / callable_this_turn / hydration_targets）投影进 load 结果。

    `live_visible_tool_names` 承载门槛层实时探针放行的名字：回合中途注册进 registry
    的工具永远不在回合起点算出的候选快照里，若只按 stale 快照判 candidate_hit，
    投影会把服务端按治理实时可见算出的 hydration_targets 清空，promotion 落成
    not_in_this_turn_candidates，前门与节点两条提升路径都会丢掉这次 load。实时放行
    名字与候选命中同权，视为可提升。
    """
    projected = dict(payload or {}) if isinstance(payload, dict) else {}
    if not projected:
        return {}
    requested_name = str(requested_tool_id or "").strip()
    resolved_name = str(projected.get("tool_id") or "").strip()
    target_names = [name for name in [requested_name, resolved_name] if name]
    runtime_payload = runtime if isinstance(runtime, dict) else {}
    candidate_names = set(_normalized_runtime_name_list(runtime_payload.get("candidate_tool_names")))
    live_visible_names = set(_normalized_runtime_name_list(live_visible_tool_names))
    callable_names = set(
        _normalized_runtime_name_list(
            runtime_payload.get("callable_tool_names")
            or runtime_payload.get("tool_names")
        )
    )
    hydrated_names = set(
        _normalized_runtime_name_list(
            runtime_payload.get("hydrated_executor_names")
            or runtime_payload.get("hydrated_tool_names")
        )
    )
    normalized_targets = _normalized_runtime_name_list(projected.get("hydration_targets"))
    candidate_hit = any(
        name in candidate_names or name in live_visible_names for name in target_names
    )
    callable_hit = any(name in callable_names for name in target_names)
    hydrated_hit = any(name in hydrated_names for name in target_names)
    projected["callable_this_turn"] = bool(callable_hit)
    if target_names:
        if candidate_hit and not callable_hit and not hydrated_hit:
            projected["will_be_hydrated_next_turn"] = bool(normalized_targets)
            projected["promotion"] = "promoted_next_turn" if normalized_targets else "not_promotable"
        else:
            normalized_targets = []
            projected["will_be_hydrated_next_turn"] = False
            if callable_hit:
                projected["promotion"] = "already_callable"
            elif hydrated_hit:
                projected["promotion"] = "already_hydrated"
            else:
                projected["promotion"] = "not_in_this_turn_candidates"
        help_text = PROMOTION_HELP.get(str(projected.get("promotion") or ""))
        if help_text:
            projected["promotion_help"] = help_text
    projected["hydration_targets"] = list(normalized_targets)
    fingerprint = build_tool_context_fingerprint(projected)
    if fingerprint:
        projected["tool_context_fingerprint"] = fingerprint
    return projected


def find_tool_record(
    tool_id: str,
    *,
    raw_tool_getter: Callable[[str], Any | None],
    resource_registry: Any,
) -> Any | None:
    needle = str(tool_id or "").strip()
    if not needle:
        return None
    record = raw_tool_getter(needle)
    if record is not None:
        return record
    for item in list(getattr(resource_registry, "list_tools", lambda: [])() or []):
        if str(getattr(item, "tool_id", "") or "").strip() == needle:
            return item
    return None


def _agent_facing_parameters_schema(*, descriptor: Any, runtime_instance: Any) -> dict[str, Any]:
    runtime_schema = normalize_object_json_schema(getattr(descriptor, "parameters", None))

    runtime_model_parameters = getattr(runtime_instance, "model_parameters", None) if runtime_instance is not None else None
    if isinstance(runtime_model_parameters, dict) and type(runtime_instance).__name__ != "RepairRequiredTool":
        return normalize_object_json_schema(runtime_model_parameters)

    metadata = dict(getattr(descriptor, "metadata", {}) or {}) if descriptor is not None else {}
    configured_model_parameters = metadata.get("model_parameters")
    if isinstance(configured_model_parameters, dict):
        return normalize_object_json_schema(configured_model_parameters)

    return runtime_schema


def build_tool_toolskill_payload(
    tool_id: str,
    *,
    raw_tool_getter: Callable[[str], Any | None],
    resource_registry: Any,
    resource_manager: Any,
) -> dict[str, Any] | None:
    """按工具名取说明正文与参数契约。工具 id 就是执行器名，没有中间层。"""
    requested_name = str(tool_id or "").strip()
    if not requested_name:
        return None

    record = find_tool_record(
        requested_name,
        raw_tool_getter=raw_tool_getter,
        resource_registry=resource_registry,
    )
    descriptor = resource_manager.get_tool_descriptor(requested_name) if resource_manager is not None else None
    if record is None and descriptor is None:
        return None

    content = ""
    path = ""
    if descriptor is not None:
        toolskill_path = getattr(descriptor, "toolskills_main_path", None)
        if toolskill_path is not None and toolskill_path.exists():
            path = str(toolskill_path)
            try:
                content = resource_manager.load_toolskill_body(requested_name)
            except FileNotFoundError:
                content = ""

    runtime_instance = resource_manager.get_tool(requested_name) if resource_manager is not None else None
    runtime_repair_required = type(runtime_instance).__name__ == "RepairRequiredTool"
    runtime_repair_reason = str(getattr(runtime_instance, "_reason", "") or "").strip() if runtime_repair_required else ""

    tool_type = str(
        getattr(descriptor, "tool_type", "")
        or getattr(record, "tool_type", "internal")
        or "internal"
    )
    install_dir = str(
        getattr(descriptor, "install_dir", None)
        or getattr(record, "install_dir", None)
        or ""
    ).strip() or None
    callable_flag = bool(getattr(descriptor, "callable", getattr(record, "callable", True)))
    available_flag = bool(getattr(descriptor, "available", getattr(record, "available", True)))
    if runtime_repair_required:
        available_flag = False

    record_metadata = dict(getattr(record, "metadata", {}) or {}) if record is not None else {}
    warnings: list[str] = []
    for item in [
        *list(getattr(descriptor, "warnings", []) or []),
        *list(record_metadata.get("warnings") or []),
    ]:
        warning = str(item or "").strip()
        if warning and warning not in warnings:
            warnings.append(warning)

    errors: list[str] = []
    for item in [
        *list(getattr(descriptor, "errors", []) or []),
        *list(record_metadata.get("errors") or []),
    ]:
        error = str(item or "").strip()
        if error and error not in errors:
            errors.append(error)
    if runtime_repair_reason and runtime_repair_reason not in errors:
        errors.append(runtime_repair_reason)

    description = str(
        getattr(descriptor, "description", "")
        or getattr(record, "description", "")
        or requested_name
    ).strip() or requested_name
    parameters_schema = _agent_facing_parameters_schema(
        descriptor=descriptor,
        runtime_instance=runtime_instance,
    )
    required_parameters = [
        str(item or "").strip()
        for item in list(parameters_schema.get("required") or [])
        if str(item or "").strip()
    ]
    exec_runtime_policy = None
    if requested_name == EXEC_TOOL_ID:
        exec_runtime_policy = resolve_exec_runtime_policy_payload(
            record=record,
            descriptor=descriptor,
        )

    return {
        "tool_id": requested_name,
        "display_name": str(getattr(record, "display_name", "") or requested_name).strip() or requested_name,
        "content": content,
        "path": path,
        "description": description,
        "tool_type": tool_type,
        "install_dir": install_dir,
        "callable": callable_flag,
        "available": available_flag,
        "repair_required": callable_flag and not available_flag,
        "parameters_schema": parameters_schema,
        "required_parameters": required_parameters,
        "parameter_contract_markdown": render_parameter_contract_markdown(parameters_schema),
        "example_arguments": build_example_from_schema(parameters_schema),
        "warnings": warnings,
        "errors": errors,
        "exec_runtime_policy": exec_runtime_policy,
    }
