"""Show one sticker category as a numbered contact sheet."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.stickers import sheet as sheet_render
from g3ku.stickers.catalog import find_pack, safe_scan_packs

SEND_TEMPLATE = "[[sticker:{pack}/{category}/<序号>]]"


class StickerSheetTool(Tool):
    """Render the category's tiles (badged with their index) into the next request."""

    hide_universal_timeout_parameter = True

    def __init__(self, runtime: Any = None):
        # 资源加载器传的是 SimpleNamespace，执行期注入的 __g3ku_runtime 是 dict。
        self._runtime = runtime

    @staticmethod
    def _runtime_value(runtime: Any, key: str, default: Any = None) -> Any:
        if isinstance(runtime, dict):
            return runtime.get(key, default)
        return getattr(runtime, key, default)

    @property
    def name(self) -> str:
        return "show_sticker_category"

    @property
    def description(self) -> str:
        return (
            "Show one sticker category as a single image: every sticker in it, each tile "
            "badged with its index. Call it when a category-level pick is too coarse and you "
            "want to choose the exact picture. The sheet is attached to THIS turn's next "
            "request only — decide and send in the same reply. To send tile N write "
            "`[[sticker:<pack>/<category>/<N>]]` as the last line; never invent an index. "
            "At most one call per reply."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pack": {"type": "string", "description": "Pack key from the sticker catalog."},
                "category": {
                    "type": "string",
                    "description": "Category key inside that pack (never a `_`-prefixed one).",
                },
            },
            "required": ["pack", "category"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        for field in ("pack", "category"):
            if not str((params or {}).get(field) or "").strip():
                errors.append(f"{field} must not be empty")
        return errors

    @staticmethod
    def _refusal(reason: str) -> str:
        return json.dumps({"ok": False, "reason": reason}, ensure_ascii=False)

    @staticmethod
    def _not_multimodal(display_name: str) -> str:
        named = f"当前模型配置名称是({display_name})，" if display_name else ""
        return json.dumps(
            {
                "ok": False,
                "error": (
                    f"{named}没有勾选多模态能力，无法拿到缩略图。"
                    "仍要发表情包就改用类别语法 `[[sticker:<包>/<类别>]]`，"
                    "系统会在该类别内随机挑一张发送；需要看图请把本会话模型切到已勾选多模态的配置。"
                ),
            },
            ensure_ascii=False,
        )

    async def execute(self, pack: str, category: str, **kwargs: Any) -> str:
        runtime = kwargs.get("__g3ku_runtime")
        if runtime is None:
            runtime = self._runtime
        display_name = str(self._runtime_value(runtime, "image_model_display_name") or "").strip()
        if not bool(self._runtime_value(runtime, "image_multimodal_enabled")):
            return self._not_multimodal(display_name)

        pack_key = str(pack or "").strip()
        category_key = str(category or "").strip()
        target = find_pack(safe_scan_packs(), pack_key)
        if target is None:
            return self._refusal(f"图库里没有包 {pack_key!r}；按契约头里的包键重试或直接按类别发送。")
        if category_key.startswith("_") or target.category(category_key) is None:
            return self._refusal(
                f"包 {pack_key!r} 里没有可用分类 {category_key!r}；按契约头里的分类键重试。"
            )

        rendered = sheet_render.render_sheet(target, category_key)
        if rendered is None:
            return self._refusal(
                f"分类 {pack_key}/{category_key} 还没有已注册的图片；"
                "请在表情包界面注册或改用其它分类。"
            )
        if rendered.count == 1:
            return json.dumps(
                {
                    "ok": False,
                    "reason": (
                        f"分类 {pack_key}/{category_key} 只有 1 张，"
                        f"直接用 {SEND_TEMPLATE.format(pack=pack_key, category=category_key)} 即可，不需要看图。"
                    ),
                },
                ensure_ascii=False,
            )

        display = f"{pack_key}-{category_key}-{rendered.sheet_id}.png"
        return json.dumps(
            {
                "ok": True,
                "content_kind": "image",
                "mime_type": rendered.mime_type,
                "multimodal_open_pending": True,
                "summary": (
                    f"分类 {pack_key}/{category_key} 的拼接图（{rendered.count} 张）"
                    "会附在本轮的下一次多模态请求里，仅此一次。"
                ),
                "runtime_image_target": {
                    "path": str(rendered.path),
                    "mime_type": rendered.mime_type,
                    "source_ref": f"sticker-sheet:{rendered.sheet_id}",
                    "display_name": display,
                },
                "pack": pack_key,
                "category": category_key,
                "count": rendered.count,
                "sheet_id": rendered.sheet_id,
                "how_to_send": (
                    f"选中后在本回复最后一行写 {SEND_TEMPLATE.format(pack=pack_key, category=category_key)}，"
                    "序号就是格子左上角的数字；不要编造序号，没合适的就正常回复不发图。"
                ),
            },
            ensure_ascii=False,
        )


__all__ = ["StickerSheetTool"]
