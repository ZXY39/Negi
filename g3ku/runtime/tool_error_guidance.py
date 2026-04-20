from __future__ import annotations


PARAMETER_ERROR_GUIDANCE_TEMPLATE = (
    '请先调用 load_tool_context(tool_id="{tool_name}") 查看该工具的详细说明、参数契约和示例后，再重新使用该工具。'
)


def parameter_error_guidance(tool_name: str) -> str:
    normalized_tool_name = str(tool_name or "").strip()
    if not normalized_tool_name:
        return ""
    return PARAMETER_ERROR_GUIDANCE_TEMPLATE.format(tool_name=normalized_tool_name)


def append_parameter_error_guidance(message: str, *, tool_name: str) -> str:
    text = str(message or "").strip()
    guidance = parameter_error_guidance(tool_name)
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
    "append_parameter_error_guidance",
    "is_parameter_like_tool_exception",
    "parameter_error_guidance",
]
