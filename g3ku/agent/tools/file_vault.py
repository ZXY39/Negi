"""File vault tools for placeholder-based file lookup and retrieval."""

from __future__ import annotations

import json
from typing import Any

from g3ku.agent.tools.base import Tool


class FileVaultLookupTool(Tool):
    """Locate previously uploaded files by query/context."""

    def __init__(self, *, vault):
        self._vault = vault

    @property
    def name(self) -> str:
        return "file_vault_lookup"

    @property
    def description(self) -> str:
        return (
            "Find uploaded files by placeholder, filename, or prior-turn context and return ranked candidates.\n"
            "MUST CALL: when user references previously uploaded files or images (e.g., '上次/之前/那个文件/那张图/那三张图/之前上传的图片/商品图/风格图') "
            "and current turn does not include the target file.\n"
            "AVOID CALL: when target file is already uploaded in current turn and can be handled directly."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Lookup query with filename or context keywords."},
                "session": {"type": "string", "description": "Optional session key override."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Max candidates."},
            },
            "required": ["query"],
        }

    async def execute(
        self,
        query: str,
        session: str | None = None,
        limit: int = 5,
        **kwargs: Any,
    ) -> str:
        runtime_raw = kwargs.pop("__g3ku_runtime", None)
        runtime = runtime_raw if isinstance(runtime_raw, dict) else {}
        session_key = str(session or runtime.get("session_key") or "") or None
        result = self._vault.lookup(query=str(query or ""), session_key=session_key, limit=limit)
        return json.dumps({"query": query, "session": session_key, "candidates": result}, ensure_ascii=False)


class FileVaultReadTool(Tool):
    """Read content of a previously uploaded file via placeholder."""

    def __init__(self, *, vault):
        self._vault = vault

    @property
    def name(self) -> str:
        return "file_vault_read"

    @property
    def description(self) -> str:
        return (
            "Read uploaded file content by placeholder.\n"
            "For images and supported files, this tool can return native multimodal content blocks back to the model.\n"
            "MUST CALL: when placeholder is known and answer requires actual file content; this includes follow-up intents like continue reading, use these placeholders, read the file itself, or ??????.\n"
            "DO NOT promise to read later when the needed placeholder is already known in the conversation; call file_vault_read in this turn.\n"
            "AVOID CALL: when only existence/list confirmation is needed without reading content."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "placeholder": {"type": "string", "description": "Canonical file placeholder."},
                "mode": {
                    "type": "string",
                    "enum": ["auto", "text", "binary_meta"],
                    "description": "Read mode.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 256,
                    "maximum": 200000,
                    "description": "Max returned chars in text mode.",
                },
            },
            "required": ["placeholder"],
        }

    async def execute(
        self,
        placeholder: str,
        mode: str = "auto",
        max_chars: int = 12000,
        **kwargs: Any,
    ) -> Any:
        _ = kwargs
        result = self._vault.read(placeholder=str(placeholder or ""), mode=mode, max_chars=max_chars)
        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and result.get("mode") == "native"
            and isinstance(result.get("content"), list)
        ):
            return result["content"]
        return json.dumps(result, ensure_ascii=False)


class FileVaultStatsTool(Tool):
    """Report file vault storage and usage stats."""

    def __init__(self, *, vault):
        self._vault = vault

    @property
    def name(self) -> str:
        return "file_vault_stats"

    @property
    def description(self) -> str:
        return "Return file vault capacity, usage, and most-used files."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "top_n": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "How many top-used files to include.",
                }
            },
        }

    async def execute(self, top_n: int = 10, **kwargs: Any) -> str:
        _ = kwargs
        return json.dumps(self._vault.stats(top_n=top_n), ensure_ascii=False)


class FileVaultSetPolicyTool(Tool):
    """Adjust file vault capacity/threshold policy."""

    def __init__(self, *, vault):
        self._vault = vault

    @property
    def name(self) -> str:
        return "file_vault_set_policy"

    @property
    def description(self) -> str:
        return (
            "Update file vault max storage and cleanup threshold. "
            "Only call when user explicitly asks to adjust storage policy."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "max_storage_gb": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 1024,
                    "description": "New max storage in GB.",
                },
                "threshold_pct": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 95,
                    "description": "Cleanup trigger threshold percentage.",
                },
            },
        }

    async def execute(
        self,
        max_storage_gb: float | None = None,
        threshold_pct: int | None = None,
        **kwargs: Any,
    ) -> str:
        _ = kwargs
        max_storage_bytes: int | None = None
        if max_storage_gb is not None:
            max_storage_bytes = int(float(max_storage_gb) * 1024 * 1024 * 1024)
        result = self._vault.set_policy(
            max_storage_bytes=max_storage_bytes,
            threshold_pct=threshold_pct,
        )
        return json.dumps(result, ensure_ascii=False)


class FileVaultCleanupTool(Tool):
    """Run file vault cleanup by frequency policy."""

    def __init__(self, *, vault):
        self._vault = vault

    @property
    def name(self) -> str:
        return "file_vault_cleanup"

    @property
    def description(self) -> str:
        return "Run file vault cleanup. Use dry_run=true first to preview deletions."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "dry_run": {"type": "boolean", "description": "Preview only without deleting files."},
                "target_pct": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 95,
                    "description": "Optional target usage percent after cleanup.",
                },
            },
        }

    async def execute(
        self,
        dry_run: bool = True,
        target_pct: int | None = None,
        **kwargs: Any,
    ) -> str:
        _ = kwargs
        result = self._vault.cleanup(dry_run=bool(dry_run), target_pct=target_pct)
        return json.dumps(result, ensure_ascii=False)

