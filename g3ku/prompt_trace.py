from __future__ import annotations

from typing import Any

_TITLE_COLOR = "\x1b[1;93m"
_ROLE_COLOR = "\x1b[1;93m"
_RESET_COLOR = "\x1b[0m"
_EMPTY = "[empty]"


def _is_text_block(item_type: str) -> bool:
    return item_type in {"text", "input_text", "output_text"}


def content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return content_to_text([value])
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
                if text:
                    parts.append(text)
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip().lower()
            if _is_text_block(item_type):
                text = item.get("text", item.get("content", ""))
                if isinstance(text, str) and text.strip():
                    parts.append(text.strip())
                continue
            if item_type in {"image_url", "input_image"}:
                parts.append("[image omitted]")
                continue
            if item_type in {"file", "input_file"}:
                filename = str(item.get("filename") or item.get("name") or "").strip()
                parts.append(f"[file omitted: {filename}]" if filename else "[file omitted]")
        return "\n".join(parts).strip()
    return str(value or "").strip()


def extract_prompt_from_messages(messages: list[dict[str, Any]]) -> tuple[str, str]:
    system_text = ""
    user_text = ""
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        if role == "system":
            system_text = content_to_text(message.get("content"))
        elif role == "user":
            user_text = content_to_text(message.get("content"))
    return system_text, user_text


def render_prompt_trace(*, system_text: str, user_text: str) -> str:
    return "\n".join(
        [
            f"{_TITLE_COLOR}=====\u672c\u8f6e\u63a5\u6536\u6700\u7ec8\u63d0\u793a\u8bcd====={_RESET_COLOR}",
            f"{_ROLE_COLOR}-----ROLE: SYSTEM-----{_RESET_COLOR}",
            system_text or _EMPTY,
            "",
            f"{_ROLE_COLOR}-----ROLE: USER-----{_RESET_COLOR}",
            user_text or _EMPTY,
        ]
    )


def render_send_data_trace(payload: str) -> str:
    return "\n".join(
        [
            f"{_TITLE_COLOR}=====\u672c\u8f6e\u53d1\u9001\u6570\u636e====={_RESET_COLOR}",
            payload or _EMPTY,
        ]
    )


def render_output_trace(output: str) -> str:
    return "\n".join(
        [
            f"{_TITLE_COLOR}=====LLM\u8f93\u51fa====={_RESET_COLOR}",
            output or _EMPTY,
        ]
    )
