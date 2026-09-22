from __future__ import annotations

from typing import Any

PARAMETER_ERROR_GUIDANCE_TEMPLATE = (
    '请先调用 load_tool_context(tool_id="{tool_name}") 查看该工具的详细说明、参数契约和示例后，再重新使用该工具。'
)

# 没有可加载契约文档的工具（如内部控制工具：无资源 descriptor / toolskill）不能
# 走 load_tool_context——调用必然被运行时合同闸门拒绝。这类工具的参数错误必须自带
# 契约：只提醒"核对必填项、类型与取值结构"而不把契约贴出来，模型没有可核对的材料，
# 下一次提交大概率撞同一堵墙。回贴以下方 schema 渲染；渲染不出来才退回这句泛化提醒。
PARAMETER_RECHECK_GUIDANCE_TEMPLATE = (
    '该工具没有可加载的扩展说明。请仔细核对该工具的入参（参数名、必填项、类型与取值结构）后重新提交。'
)

_PARAMETER_CONTRACT_PREFIX = '该工具没有可加载的扩展说明，参数契约如下（必填项及其类型与取值结构）：'
_PARAMETER_CONTRACT_OPTIONAL_PREFIX = '。可选：'
# 契约文本随每条参数错误进上下文，必须有界；控制工具的 schema 很小，超限即视为
# 渲染不适用于该工具，退回泛化提醒而不是截断出一份自相矛盾的契约。
_PARAMETER_CONTRACT_MAX_CHARS = 800


def tool_supports_context_load(tool: Any) -> bool:
    """工具是否具备 load_tool_context 可加载的资源契约。

    资源工具实例（ManifestBackedTool / EmbeddedMCPTool / RepairRequiredTool）
    携带 `_descriptor`，其参数契约/工具说明可经 load_tool_context 加载；纯内部
    工具（如 spawn_child_nodes / submit_next_stage 等运行时注入的控制工具）没有
    资源条目，加载必然失败。
    """
    return tool is not None and getattr(tool, "_descriptor", None) is not None


def _base_type_label(schema: dict[str, Any]) -> str:
    raw_types = schema.get("type")
    candidates = (
        [raw_types]
        if isinstance(raw_types, str)
        else [item for item in (raw_types or []) if isinstance(item, str)]
    )
    names = [str(item).strip() for item in candidates if str(item or "").strip()]
    label = "|".join(names) if names else "any"
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        label += "(" + "|".join(str(item) for item in enum[:8]) + ")"
    min_length = schema.get("minLength")
    if isinstance(min_length, int) and not isinstance(min_length, bool) and min_length > 0:
        label += "(非空)" if min_length == 1 else f"(len>={min_length})"
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        label += f"(>={minimum})"
    return label


def _object_shape_suffix(schema: dict[str, Any]) -> str:
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return "<object>"
    required = [str(item).strip() for item in (schema.get("required") or []) if str(item or "").strip()]
    parts = [
        f"{name}={_base_type_label(properties[name])}"
        for name in required
        if isinstance(properties.get(name), dict)
    ]
    optional = [name for name in properties if name not in set(required)]
    body = ""
    if parts:
        body += "必填:" + ",".join(parts)
    if optional:
        body += (";" if body else "") + "可选:" + ",".join(str(name) for name in optional)
    return f"<object{{{body}}}>"


def _contract_type_label(schema: dict[str, Any]) -> str:
    label = _base_type_label(schema)
    if "array" not in label:
        return label
    items_schema = schema.get("items")
    if not isinstance(items_schema, dict):
        return label
    if str(items_schema.get("type") or "").strip().lower() == "object":
        return label + _object_shape_suffix(items_schema)
    return label.replace("array", f"array<{_base_type_label(items_schema)}>", 1)


def _render_parameter_contract(tool: Any) -> str:
    """Render the authoritative parameter contract, or '' when not renderable.

    以 `Tool.parameters` 为准（`validate_params` 拒收时用的就是它），而不是可能被
    裁剪过的 model-visible `model_parameters`——模型要过的是校验面。
    """
    try:
        schema = getattr(tool, "parameters", None)
        if not isinstance(schema, dict):
            return ""
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return ""
        required = [str(item).strip() for item in (schema.get("required") or []) if str(item or "").strip()]
        if not required:
            return ""
        chunks: list[str] = []
        for name in required:
            spec = properties.get(name)
            chunks.append(f"{name}={_contract_type_label(spec) if isinstance(spec, dict) else 'any'}")
        text = _PARAMETER_CONTRACT_PREFIX + "、".join(chunks)
        optional = [str(name) for name in properties if str(name) not in set(required)]
        if optional:
            text += _PARAMETER_CONTRACT_OPTIONAL_PREFIX + "、".join(optional)
        if len(text) > _PARAMETER_CONTRACT_MAX_CHARS:
            return ""
        return text
    except Exception:
        return ""


def parameter_error_guidance(tool_name: str, *, tool: Any | None = None) -> str:
    normalized_tool_name = str(tool_name or "").strip()
    if not normalized_tool_name:
        return ""
    if tool is not None and not tool_supports_context_load(tool):
        return _render_parameter_contract(tool) or PARAMETER_RECHECK_GUIDANCE_TEMPLATE
    return PARAMETER_ERROR_GUIDANCE_TEMPLATE.format(tool_name=normalized_tool_name)


def append_parameter_error_guidance(message: str, *, tool_name: str, tool: Any | None = None) -> str:
    text = str(message or "").strip()
    guidance = parameter_error_guidance(tool_name, tool=tool)
    if not guidance:
        return text
    if guidance in text:
        return text
    if not text:
        return guidance
    return f"{text}\n{guidance}"


def is_parameter_like_tool_exception(exc: BaseException | None) -> bool:
    return isinstance(exc, (TypeError, ValueError))


__all__ = [
    "PARAMETER_ERROR_GUIDANCE_TEMPLATE",
    "PARAMETER_RECHECK_GUIDANCE_TEMPLATE",
    "append_parameter_error_guidance",
    "is_parameter_like_tool_exception",
    "parameter_error_guidance",
    "tool_supports_context_load",
]
