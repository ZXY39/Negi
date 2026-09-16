from __future__ import annotations

from typing import Any

PARAMETER_ERROR_GUIDANCE_TEMPLATE = (
    '请先调用 load_tool_context(tool_id="{tool_name}") 查看该工具的详细说明、参数契约和示例后，再重新使用该工具。'
)

# 没有可加载契约文档的工具（如内部控制工具：无资源 descriptor / toolskill）不能
# 走 load_tool_context——调用必然被运行时合同闸门拒绝。这类工具的参数错误改为
# 提醒模型直接核对入参，避免把模型推进一条必死的加载链路。
PARAMETER_RECHECK_GUIDANCE_TEMPLATE = (
    '该工具没有可加载的扩展说明。请仔细核对该工具的入参（参数名、必填项、类型与取值结构）后重新提交。'
)


def tool_supports_context_load(tool: Any) -> bool:
    """工具是否具备 load_tool_context 可加载的资源契约。

    资源工具实例（ManifestBackedTool / EmbeddedMCPTool / RepairRequiredTool）
    携带 `_descriptor`，其参数契约/工具说明可经 load_tool_context 加载；纯内部
    工具（如 spawn_child_nodes / submit_next_stage 等运行时注入的控制工具）没有
    资源条目，加载必然失败。
    """
    return tool is not None and getattr(tool, "_descriptor", None) is not None


def parameter_error_guidance(tool_name: str, *, tool: Any | None = None) -> str:
    normalized_tool_name = str(tool_name or "").strip()
    if not normalized_tool_name:
        return ""
    if tool is not None and not tool_supports_context_load(tool):
        return PARAMETER_RECHECK_GUIDANCE_TEMPLATE
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
