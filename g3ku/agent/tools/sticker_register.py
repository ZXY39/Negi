"""Register sticker files that were dropped into a pack folder."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.stickers import registry
from g3ku.stickers.catalog import find_pack, safe_scan_packs


class StickerRegisterTool(Tool):
    """Turn hand-dropped files into sendable stickers (``NNN.<ext>`` numbering)."""

    hide_universal_timeout_parameter = True

    @property
    def name(self) -> str:
        return "register_stickers"

    @property
    def description(self) -> str:
        return (
            "Register images that were copied into a sticker pack folder. Files sitting in a "
            "category are NOT sendable until registered; registration renames them to the "
            "next free index (001, 002, …) and they become visible to the model. Omit "
            "`category` to register every pending file in the pack. A category is capped at "
            "36 images — split it instead of forcing more in."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pack": {"type": "string", "description": "Pack key from the sticker catalog."},
                "category": {
                    "type": "string",
                    "description": "Optional category key; omit to register all categories.",
                },
            },
            "required": ["pack"],
        }

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        if not str((params or {}).get("pack") or "").strip():
            errors.append("pack must not be empty")
        return errors

    async def execute(self, pack: str, category: str = "", **kwargs: Any) -> str:
        _ = kwargs
        pack_key = str(pack or "").strip()
        target = find_pack(safe_scan_packs(), pack_key)
        if target is None:
            return json.dumps(
                {"ok": False, "reason": f"图库里没有包 {pack_key!r}"}, ensure_ascii=False
            )
        registry.ensure_bootstrap(target.path)
        wanted = str(category or "").strip()
        if wanted:
            categories = [wanted]
        else:
            categories = sorted(registry.category_summary(target.path))

        registered: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for name in categories:
            for path in registry.unregistered_files(target.path, name):
                try:
                    row = registry.register_existing(target.path, name, path)
                except registry.RegistryError as exc:
                    errors.append({"category": name, "file": path.name, "reason": exc.message})
                    continue
                registered.append(
                    {"category": name, "name": row["name"], "index": row["index"]}
                )
        return json.dumps(
            {
                "ok": True,
                "pack": pack_key,
                "registered": registered,
                "errors": errors,
                "pending": sum(
                    len(registry.unregistered_files(target.path, name)) for name in categories
                ),
                "limit_per_category": registry.MAX_IMAGES_PER_CATEGORY,
                "how_to_send": (
                    "注册后可用 `[[sticker:<包>/<分类>/<序号>]]` 精确发送，"
                    "或 `[[sticker:<包>/<分类>]]` 随机发一张。"
                ),
            },
            ensure_ascii=False,
        )


__all__ = ["StickerRegisterTool"]
