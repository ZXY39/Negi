from __future__ import annotations

import asyncio
import json
from typing import Any

from g3ku.agent.tools.agent_browser_client import AgentBrowserClient
from g3ku.agent.tools.base import Tool


class AgentBrowserTool(Tool):
    """Browser automation wrapper around the external `agent-browser` CLI."""

    def __init__(self, defaults: dict[str, Any] | None = None):
        self._defaults = dict(defaults or {})
        self._client = AgentBrowserClient(defaults=self._defaults)

    @property
    def name(self) -> str:
        return "agent_browser"

    @property
    def description(self) -> str:
        return (
            "Browser automation powered by the external agent-browser CLI. "
            "Use this tool when the user asks to open sites, search, click, fill forms, log in, read page text, take screenshots, or download files. "
            "After opening a page, use snapshot before interacting, and re-run snapshot after navigation or DOM changes. "
            "When the user explicitly asks to open a visible browser or watch browser actions, call this tool with headless=false. "
            "Use headless=true only for background probing or silent browser tasks."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Primary agent-browser command, e.g. open, snapshot, click, fill, get, wait, cookies, storage, screenshot, state, or close.",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Positional arguments for the command. Example: command=get, args=[text, @e1].",
                },
                "session": {
                    "type": "string",
                    "description": "Optional browser session override. Defaults to the current Nano session.",
                },
                "headless": {
                    "type": "boolean",
                    "description": "Whether to run in background mode. Use false when the user wants to see the browser window.",
                },
                "timeout_s": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 3600,
                    "description": "Optional command timeout in seconds.",
                },
            },
            "required": ["command"],
        }

    async def execute(
        self,
        command: str,
        args: list[str] | None = None,
        session: str | None = None,
        headless: bool | None = None,
        timeout_s: int | None = None,
        **kwargs: Any,
    ) -> str:
        runtime = kwargs.pop("__g3ku_runtime", None) or {}
        session_key = str(session or runtime.get("session_key") or "default")
        progress_cb = runtime.get("on_progress") if isinstance(runtime, dict) else None
        trace_meta = runtime.get("trace_meta") if isinstance(runtime, dict) else None
        temp_dir = str(runtime.get("temp_dir") or "").strip()

        async def progress_with_trace(text: str, *, event_kind: str | None = None, event_data: dict[str, Any] | None = None):
            if not progress_cb:
                return None
            merged: dict[str, Any] = dict(trace_meta) if isinstance(trace_meta, dict) else {}
            if isinstance(event_data, dict):
                merged.update(event_data)
            merged.setdefault("tool_name", "agent_browser")
            try:
                result = progress_cb(text, event_kind=event_kind, event_data=merged or None)
            except TypeError:
                result = progress_cb(text)
            if asyncio.iscoroutine(result):
                return await result
            return result

        result = await self._client.run(
            command=str(command or ""),
            args=[str(v) for v in (args or [])],
            session=session_key,
            headless=headless,
            timeout_s=timeout_s,
            extra_env={"G3KU_TMP_DIR": temp_dir} if temp_dir else None,
            on_progress=progress_with_trace if progress_cb else None,
        )
        return json.dumps(result, ensure_ascii=False)


