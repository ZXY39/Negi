"""图片能力降级的唯一归属地：模型不收图时，把图留在上下文里当"可取回的路径 + 原因"。

两车道共用，分工是刻意的：

- **装配层**（前门/节点拼图道）手里有上传记录的绝对路径，降级文本在那里生成，模型能直接
  用 `content_open` 或换配置后再取。
- **咽喉点**（`main/runtime/chat_backend.py` 每次 provider attempt 之前）只兜"仍有 image 块
  漏到这一发"的网：它拿不到上传记录，路径写 `(inline)`。

降级**不是失败**：不抛错、不让模型链前进、不换成员——图收不收与"这一发该用哪颗模型"是两条
独立的判断，混在一起会让一次带图请求把整条链烧穿。运行时也绝不因为图去改模型配置。
"""

from __future__ import annotations

from typing import Any, Callable

# 咽喉点没有上传记录可查：留一个明写的占位，而不是编一个不存在的路径。
INLINE_IMAGE_PATH_PLACEHOLDER = "(inline)"

_IMAGE_BLOCK_TYPES = frozenset({"image_url", "input_image"})


def build_image_degradation_text(*, path: str, display_name: str) -> str:
    """一行降级说明：路径 + 点名当前模型配置 + 下一步怎么走。"""

    return (
        f"[图片未进请求] {str(path or INLINE_IMAGE_PATH_PLACEHOLDER).strip()} "
        f"—— 当前模型配置名称是({display_name})没有勾选多模态能力因此无法看图；"
        f"需要看图请改用 content_open，或换到已勾选多模态的模型配置。"
    )


def _block_image_url(block: dict[str, Any]) -> str:
    image_value = block.get("image_url")
    if isinstance(image_value, dict):
        return str(image_value.get("url") or "").strip()
    return str(image_value or block.get("url") or "").strip()


def message_content_has_image_blocks(content: Any) -> bool:
    """这条 message content 里是否还有图片块（字符串/None 恒 False）。"""

    if not isinstance(content, list):
        return False
    return any(
        isinstance(block, dict) and str(block.get("type") or "").strip().lower() in _IMAGE_BLOCK_TYPES
        for block in content
    )


def degrade_image_blocks_in_messages(
    messages: list[dict[str, Any]] | None,
    *,
    display_name: str,
    path_lookup: Callable[[dict[str, Any]], str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """把消息里的图片块原地换成文本块；返回 ``(新消息, 换掉的块数)``。

    没有图片块时原样返回同一批 dict（调用方可以无脑传），文本块并进同一条消息的
    content 列表里，保持"一条消息一段图说明"的形状，不额外插消息——插消息会动
    append-only 前缀的跳数。
    """

    records = list(messages or [])
    degraded = 0
    output: list[dict[str, Any]] = []
    for record in records:
        item = record if isinstance(record, dict) else {}
        content = item.get("content")
        if not message_content_has_image_blocks(content):
            output.append(item)
            continue
        blocks: list[dict[str, Any]] = []
        notes: list[str] = []
        for block in list(content or []):
            if isinstance(block, dict) and str(block.get("type") or "").strip().lower() in _IMAGE_BLOCK_TYPES:
                path = ""
                if callable(path_lookup):
                    path = str(path_lookup(block) or "").strip()
                if not path:
                    path = INLINE_IMAGE_PATH_PLACEHOLDER
                notes.append(build_image_degradation_text(path=path, display_name=display_name))
                degraded += 1
                continue
            blocks.append(block)
        text_parts = [
            str(block.get("text") or "").strip()
            for block in blocks
            if isinstance(block, dict) and str(block.get("type") or "").strip().lower() == "text"
        ]
        leading_text = "\n".join([part for part in text_parts if part])
        merged_text = "\n".join([part for part in [leading_text, *notes] if part])
        remaining_blocks = [block for block in blocks if not (
            isinstance(block, dict) and str(block.get("type") or "").strip().lower() == "text"
        )]
        if merged_text:
            remaining_blocks.insert(0, {"type": "text", "text": merged_text})
        updated = dict(item)
        updated["content"] = remaining_blocks if remaining_blocks else merged_text
        output.append(updated)
    return output, degraded


__all__ = [
    "INLINE_IMAGE_PATH_PLACEHOLDER",
    "build_image_degradation_text",
    "degrade_image_blocks_in_messages",
    "message_content_has_image_blocks",
]
