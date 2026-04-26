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
    EXEC_TOOL_EXECUTOR_NAME,
    EXEC_TOOL_FAMILY_ID,
    resolve_exec_runtime_policy_payload,
)


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
        "callable": bool(normalized.get("callable")),
        "available": bool(normalized.get("available")),
        "repair_required": bool(normalized.get("repair_required")),
        "callable_now": bool(normalized.get("callable_now")),
        "will_be_hydrated_next_turn": bool(normalized.get("will_be_hydrated_next_turn")),
        "hydration_targets": _normalized_runtime_name_list(normalized.get("hydration_targets")),
        "exec_runtime_policy": (
            dict(normalized.get("exec_runtime_policy") or {})
            if isinstance(normalized.get("exec_runtime_policy"), dict)
            else None
        ),
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
) -> dict[str, Any]:
    projected = dict(payload or {}) if isinstance(payload, dict) else {}
    if not projected:
        return {}
    requested_name = str(requested_tool_id or "").strip()
    resolved_name = str(projected.get("tool_id") or "").strip()
    target_names = [name for name in [requested_name, resolved_name] if name]
    runtime_payload = runtime if isinstance(runtime, dict) else {}
    candidate_names = set(_normalized_runtime_name_list(runtime_payload.get("candidate_tool_names")))
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
    candidate_hit = any(name in candidate_names for name in target_names)
    callable_hit = any(name in callable_names for name in target_names)
    hydrated_hit = any(name in hydrated_names for name in target_names)
    if target_names:
        if not candidate_hit or callable_hit or hydrated_hit:
            normalized_targets = []
            projected["will_be_hydrated_next_turn"] = False
        else:
            projected["will_be_hydrated_next_turn"] = bool(normalized_targets)
    projected["hydration_targets"] = list(normalized_targets)
    fingerprint = build_tool_context_fingerprint(projected)
    if fingerprint:
        projected["tool_context_fingerprint"] = fingerprint
    return projected


def find_tool_family(
    tool_id: str,
    *,
    raw_tool_family_getter: Callable[[str], Any | None],
    resource_registry: Any,
) -> Any | None:
    needle = str(tool_id or "").strip()
    if not needle:
        return None
    family = raw_tool_family_getter(needle)
    if family is not None:
        return family
    for item in list(getattr(resource_registry, "list_tool_families", lambda: [])() or []):
        action_names = {
            str(executor_name or "").strip()
            for action in list(getattr(item, "actions", []) or [])
            for executor_name in list(getattr(action, "executor_names", []) or [])
            if str(executor_name or "").strip()
        }
        if needle in action_names:
            return item
    return None


def resolve_primary_executor_name(family: Any, *, resource_manager: Any) -> str:
    primary = str(getattr(family, "primary_executor_name", "") or "").strip()
    if primary:
        return primary
    for action in list(getattr(family, "actions", []) or []):
        for executor_name in list(getattr(action, "executor_names", []) or []):
            name = str(executor_name or "").strip()
            if name:
                return name
    fallback = str(getattr(family, "tool_id", "") or "").strip()
    descriptor = resource_manager.get_tool_descriptor(fallback) if resource_manager is not None and fallback else None
    if descriptor is not None:
        return fallback
    return ""


def _agent_facing_parameters_schema(*, descriptor: Any, runtime_instance: Any) -> dict[str, Any]:
    runtime_schema = normalize_object_json_schema(getattr(descriptor, "parameters", None))

    runtime_model_parameters = getattr(runtime_instance, "model_parameters", None) if runtime_instance is not None else None
    if isinstance(runtime_model_parameters, dict) and type(runtime_instance).__name__ != "RepairRequiredTool":
        return normalize_object_json_schema(runtime_model_parameters)

    metadata = dict(getattr(descriptor, "metadata", {}) or {})
    configured_model_parameters = metadata.get("model_parameters")
    if isinstance(configured_model_parameters, dict):
        return normalize_object_json_schema(configured_model_parameters)

    return runtime_schema


def build_tool_toolskill_payload(
    tool_id: str,
    *,
    raw_tool_family_getter: Callable[[str], Any | None],
    resource_registry: Any,
    resource_manager: Any,
) -> dict[str, Any] | None:
    requested_name = str(tool_id or "").strip()
    if not requested_name:
        return None

    family = find_tool_family(
        requested_name,
        raw_tool_family_getter=raw_tool_family_getter,
        resource_registry=resource_registry,
    )

    requested_descriptor = resource_manager.get_tool_descriptor(requested_name) if resource_manager is not None else None
    if family is None and requested_descriptor is None:
        return None

    primary_executor_name = (
        resolve_primary_executor_name(family, resource_manager=resource_manager)
        if family is not None
        else requested_name
    )

    candidate_names: list[str] = []
    for candidate in (
        requested_name,
        primary_executor_name,
        str(getattr(family, "tool_id", "") or "").strip() if family is not None else "",
    ):
        normalized = str(candidate or "").strip()
        if normalized and normalized not in candidate_names:
            candidate_names.append(normalized)

    content = ""
    path = ""
    toolskill_source_name = ""
    source_descriptor = None
    for candidate in candidate_names:
        if resource_manager is None:
            break
        descriptor = resource_manager.get_tool_descriptor(candidate)
        if descriptor is None:
            continue
        if source_descriptor is None:
            source_descriptor = descriptor
            toolskill_source_name = candidate
        toolskill_path = getattr(descriptor, "toolskills_main_path", None)
        if toolskill_path is None or not toolskill_path.exists():
            continue
        try:
            content = resource_manager.load_toolskill_body(candidate)
        except FileNotFoundError:
            content = ""
        path = str(toolskill_path)
        source_descriptor = descriptor
        toolskill_source_name = candidate
        break

    if source_descriptor is None:
        source_descriptor = requested_descriptor
    if not path and source_descriptor is not None and getattr(source_descriptor, "toolskills_main_path", None) is not None:
        path = str(source_descriptor.toolskills_main_path)

    effective_descriptor = requested_descriptor or source_descriptor
    runtime_instance_name = requested_name if requested_descriptor is not None else toolskill_source_name
    runtime_instance = resource_manager.get_tool(runtime_instance_name) if resource_manager is not None and runtime_instance_name else None
    runtime_repair_required = type(runtime_instance).__name__ == "RepairRequiredTool"
    runtime_repair_reason = str(getattr(runtime_instance, "_reason", "") or "").strip() if runtime_repair_required else ""

    tool_type = str(
        getattr(effective_descriptor, "tool_type", "")
        or getattr(family, "tool_type", "internal")
        or "internal"
    )
    install_dir = str(
        getattr(effective_descriptor, "install_dir", None)
        or getattr(family, "install_dir", None)
        or ""
    ).strip() or None
    callable_flag = bool(getattr(effective_descriptor, "callable", getattr(family, "callable", True)))
    available_flag = bool(getattr(effective_descriptor, "available", getattr(family, "available", True)))
    if runtime_repair_required:
        available_flag = False

    warnings: list[str] = []
    for item in list(getattr(effective_descriptor, "warnings", []) or []):
        warning = str(item or "").strip()
        if warning and warning not in warnings:
            warnings.append(warning)
    for item in list(getattr(family, "metadata", {}).get("warnings") or []):
        warning = str(item or "").strip()
        if warning and warning not in warnings:
            warnings.append(warning)

    errors: list[str] = []
    for item in list(getattr(effective_descriptor, "errors", []) or []):
        error = str(item or "").strip()
        if error and error not in errors:
            errors.append(error)
    for item in list(getattr(family, "metadata", {}).get("errors") or []):
        error = str(item or "").strip()
        if error and error not in errors:
            errors.append(error)
    if runtime_repair_reason and runtime_repair_reason not in errors:
        errors.append(runtime_repair_reason)

    family_tool_id = str(getattr(family, "tool_id", "") or "").strip() if family is not None else ""
    description = str(
        getattr(effective_descriptor, "description", "")
        or getattr(family, "description", "")
        or requested_name
    ).strip() or requested_name
    parameters_schema = _agent_facing_parameters_schema(
        descriptor=effective_descriptor,
        runtime_instance=runtime_instance,
    )
    required_parameters = [
        str(item or "").strip()
        for item in list(parameters_schema.get("required") or [])
        if str(item or "").strip()
    ]
    exec_runtime_policy = None
    if requested_name in {EXEC_TOOL_FAMILY_ID, EXEC_TOOL_EXECUTOR_NAME} or family_tool_id == EXEC_TOOL_FAMILY_ID:
        exec_runtime_policy = resolve_exec_runtime_policy_payload(
            family=family,
            descriptor=effective_descriptor,
        )

    return {
        "tool_id": requested_name,
        "family_tool_id": family_tool_id or requested_name,
        "primary_executor_name": primary_executor_name,
        "toolskill_source_name": toolskill_source_name or requested_name,
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
