from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

PARAMETER_ERROR_GUIDANCE_TEMPLATE = (
    '请先调用 load_tool_context(tool_id="{tool_name}") 查看该工具的详细说明、参数契约和示例后，再重新使用该工具。'
)

# 说明已在本轮上下文里的工具不能被再引导去 load_tool_context——同版本重复读取会被
# 守卫拒绝（节点道与 frontdoor 的「禁止重复读取」），指针于是指向一条必死调用。
# 这种情形改为给出 toolskill 文件路径，让模型用 content_open 定点读需要的那一段。
PARAMETER_SKILL_REOPEN_GUIDANCE_TEMPLATE = (
    '该工具的说明已在本轮上下文中，重复 load_tool_context 会被拒绝。'
    '请改用 content_open(path="{path}") 查看参数契约与示例后再重新提交。'
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
_UNRECOGNIZED_KEYS_PREFIX = '本次提交中该工具不接受这些参数名：'
_UNRECOGNIZED_KEYS_SUFFIX = '；它们不会进入该工具，请改用上方契约里的参数名。'
_UNRECOGNIZED_KEYS_MAX = 5
# 提交里一个参数都没有时，"缺全部必填字段"有两种可能：模型真发了空对象，或参数
# JSON 在解析处失败被静默降级成 {}（`_normalize_tool_call_arguments` 与
# `base_chat_model_adapter` 都这么做）。两者无法从结果区分，所以把第二种可能明说，
# 别让模型逐字段去补一个根本没收到的参数串。
_EMPTY_SUBMISSION_NOTE = (
    '本次调用未携带任何参数：如果你确实写了参数内容，说明参数 JSON 没有被成功解析，'
    '请压缩内容后按下方结构重新提交。'
)
_AVAILABILITY_CALLABLE_PREFIX = '当前可直接调用的工具：'
_AVAILABILITY_CANDIDATE_PREFIX = '本轮可加载的候选工具：'
_AVAILABILITY_NAME_MAX = 20
_NOT_HYDRATED_NOTE = (
    '「{name}」本轮只是候选、尚未水化，因此不可直接调用；'
    '请先 load_tool_context(tool_id="{name}") 后再使用。'
)
_NO_LOAD_NEEDED_NOTE = '「{name}」是常驻内置工具，无需加载说明即可直接调用；它没有可加载的 toolskill。'


def tool_supports_context_load(tool: Any) -> bool:
    """工具是否具备 load_tool_context 可加载的资源契约。

    资源工具实例（ManifestBackedTool / EmbeddedMCPTool / RepairRequiredTool）
    携带 `_descriptor`，其参数契约/工具说明可经 load_tool_context 加载；纯内部
    工具（如 spawn_child_nodes / submit_next_stage 等运行时注入的控制工具）没有
    资源条目，加载必然失败。
    """
    return tool is not None and getattr(tool, "_descriptor", None) is not None


def _availability_name_list(names: Any) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_name in list(names or []):
        name = str(raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        ordered.append(name)
    return ordered


def format_name_group(prefix: str, names: Any, *, limit: int = _AVAILABILITY_NAME_MAX) -> str:
    """Render one capped `前缀：a、b、c 等 N 个` group, or '' when there is nothing to list.

    公开给同一条修复道上的其他错误文本（`load_*` 的门禁拒绝等）复用，避免各处
    自己拼名单、各自定上限。
    """
    ordered = _availability_name_list(names)
    if not ordered:
        return ""
    shown = ordered[:limit]
    tail = f" 等 {len(ordered)} 个" if len(ordered) > limit else ""
    return prefix + "、".join(shown) + tail


def _format_availability_group(prefix: str, names: list[str]) -> str:
    return format_name_group(prefix, names)


def availability_hint(*, requested: str, callable_names: Any = None, candidate_names: Any = None) -> str:
    """名称被拒时说明"什么能用"：候选未水化与纯未知名的修法不同，必须分开讲。"""
    name = str(requested or "").strip()
    callable_list = _availability_name_list(callable_names)
    candidate_list = _availability_name_list(candidate_names)
    parts: list[str] = []
    if name and name in set(candidate_list):
        parts.append(_NOT_HYDRATED_NOTE.format(name=name))
    group = _format_availability_group(_AVAILABILITY_CALLABLE_PREFIX, callable_list)
    if group:
        parts.append(group)
    unseen_candidates = [item for item in candidate_list if item not in set(callable_list)]
    group = _format_availability_group(_AVAILABILITY_CANDIDATE_PREFIX, unseen_candidates)
    if group:
        parts.append(group)
    return "；".join(parts)


def no_load_needed_hint(*, requested: str, actor_role: str = "") -> str:
    """常驻内置工具被送去 load_tool_context 时，先回答"它不用加载"再列候选。"""
    name = str(requested or "").strip()
    if not name:
        return ""
    try:
        from g3ku.runtime.tool_visibility import fixed_builtin_tool_name_set_for_actor_role

        builtin_names = fixed_builtin_tool_name_set_for_actor_role(str(actor_role or "").strip())
    except Exception:
        return ""
    return _NO_LOAD_NEEDED_NOTE.format(name=name) if name in builtin_names else ""


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


def _schema_properties(tool: Any) -> dict[str, Any]:
    try:
        schema = getattr(tool, "parameters", None)
        if not isinstance(schema, dict):
            return {}
        properties = schema.get("properties")
        return properties if isinstance(properties, dict) else {}
    except Exception:
        return {}


def _render_unrecognized_keys_hint(tool: Any, arguments: Any) -> str:
    """Name the parameters the schema does not define, with near-miss suggestions.

    校验器只遍历 schema 认识的键、多余键静默放过，所以键名拼错的表象是
    "missing required X" 而不是"你把 X 拼错了"：模型看不出因果，只能整段重写。
    本提示不改变校验结论（这些键今天同样被忽略），只补因果。
    """
    properties = _schema_properties(tool)
    if not properties or not isinstance(arguments, dict):
        return ""
    known = [str(name) for name in properties]
    unknown = [str(name) for name in arguments if str(name).strip() and str(name) not in properties]
    if not unknown:
        return ""
    parts: list[str] = []
    for name in unknown[:_UNRECOGNIZED_KEYS_MAX]:
        near = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
        parts.append(f"{name}→{near[0]}?" if near else name)
    if len(unknown) > _UNRECOGNIZED_KEYS_MAX:
        parts.append(f"等 {len(unknown)} 个")
    return _UNRECOGNIZED_KEYS_PREFIX + "、".join(parts) + _UNRECOGNIZED_KEYS_SUFFIX


def _toolskill_path(tool: Any) -> str:
    try:
        path = getattr(getattr(tool, "_descriptor", None), "toolskills_main_path", None)
        if path is None:
            return ""
        text = str(path).strip()
        if not text:
            return ""
        return text if Path(text).exists() else ""
    except Exception:
        return ""


def _tool_context_names(runtime_context: Any, *keys: str) -> set[str]:
    if not isinstance(runtime_context, dict):
        return set()
    names: set[str] = set()
    for key in keys:
        value = runtime_context.get(key)
        if isinstance(value, (list, tuple, set)):
            names.update(str(item or "").strip() for item in value if str(item or "").strip())
    return names


def _parameter_repair_pointer(tool_name: str, *, tool: Any, runtime_context: Any) -> str:
    """说明文档指针三分支：已在上下文 → content_open 定点读；否则 → load_tool_context。"""
    normalized_tool_name = str(tool_name or "").strip()
    path = _toolskill_path(tool) if tool is not None else ""
    hydrated = _tool_context_names(runtime_context, "hydrated_executor_names", "hydrated_tool_names")
    if path and normalized_tool_name in hydrated:
        return PARAMETER_SKILL_REOPEN_GUIDANCE_TEMPLATE.format(path=path)
    return PARAMETER_ERROR_GUIDANCE_TEMPLATE.format(tool_name=normalized_tool_name)


def parameter_error_guidance(
    tool_name: str,
    *,
    tool: Any | None = None,
    arguments: Any = None,
    runtime_context: Any = None,
) -> str:
    """Compose the repair material for a tool parameter error.

    顺序固定为「本次错在哪」在前、「去哪儿看契约」在后：前者解释这一跳为什么失败，
    后者只是取材料的路径；只有指针而没有前者时，模型知道自己要重读却不知该改什么。
    """
    normalized_tool_name = str(tool_name or "").strip()
    hint = _render_unrecognized_keys_hint(tool, arguments) if tool is not None else ""
    if not normalized_tool_name:
        return hint
    if tool is not None and not tool_supports_context_load(tool):
        body = _render_parameter_contract(tool) or PARAMETER_RECHECK_GUIDANCE_TEMPLATE
        # 「按下方结构」只有在契约真被内联到下面时才成立，故不挂到指针分支上。
        if body is not PARAMETER_RECHECK_GUIDANCE_TEMPLATE and isinstance(arguments, dict) and not arguments:
            hint = "\n".join([item for item in (_EMPTY_SUBMISSION_NOTE, hint) if item])
    else:
        body = _parameter_repair_pointer(normalized_tool_name, tool=tool, runtime_context=runtime_context)
    guidance = "\n".join([item for item in (hint, body) if item])
    return guidance or body


def append_parameter_error_guidance(
    message: str,
    *,
    tool_name: str,
    tool: Any | None = None,
    arguments: Any = None,
    runtime_context: Any = None,
) -> str:
    text = str(message or "").strip()
    guidance = parameter_error_guidance(
        tool_name,
        tool=tool,
        arguments=arguments,
        runtime_context=runtime_context,
    )
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
    "PARAMETER_SKILL_REOPEN_GUIDANCE_TEMPLATE",
    "append_parameter_error_guidance",
    "availability_hint",
    "format_name_group",
    "is_parameter_like_tool_exception",
    "no_load_needed_hint",
    "parameter_error_guidance",
    "tool_supports_context_load",
]
