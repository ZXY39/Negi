"""Memory system for persistent agent memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from g3ku.agent.chatmodel_utils import ensure_chat_model

from g3ku.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from g3ku.session.manager import Session


_SAVE_MEMORY_TOOL = {
    "name": "save_memory",
    "description": "Save the memory consolidation result to persistent storage.",
}


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## 长期记忆\n{long_term}" if long_term else ""

    async def consolidate(
        self,
        session: Session,
        provider: Any,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into MEMORY.md + HISTORY.md via official tool-calling."""
        if archive_all:
            old_messages = session.messages
            keep_count = 0
            logger.info("Memory consolidation (archive_all): {} messages", len(session.messages))
        else:
            keep_count = memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            if len(session.messages) - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:-keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation: {} to consolidate, {} keep", len(old_messages), keep_count)

        lines = []
        for msg in old_messages:
            if not msg.get("content"):
                continue
            tools = f" [tools: {', '.join(msg['tools_used'])}]" if msg.get("tools_used") else ""
            lines.append(f"[{msg.get('timestamp', '?')[:16]}] {msg['role'].upper()}{tools}: {msg['content']}")

        current_memory = self.read_long_term()
        prompt = f"""处理此对话，并使用你的整理结果调用 save_memory 工具。

## 当前长期记忆
{current_memory or "(空)"}

## 待处理的对话
{chr(10).join(lines)}"""

        try:
            from langchain.tools import tool

            @tool
            def save_memory(history_entry: str, memory_update: str) -> str:
                """Persist consolidated history entry and updated long-term memory markdown."""

                return "saved"

            model_client = ensure_chat_model(
                provider,
                default_model=model,
                default_temperature=0.0,
                default_max_tokens=max(memory_window * 64, 512),
                default_reasoning_effort=None,
            )
            bound_model = model_client.bind_tools([save_memory])
            response = await bound_model.ainvoke(
                [
                    {
                        "role": "system",
                        "content": "你是一个记忆整理代理。请使用你整理的对话调用 save_memory 工具。",
                    },
                    {"role": "user", "content": prompt},
                ]
            )

            tool_payload: dict[str, Any] = {}
            for tool_call in (getattr(response, "tool_calls", None) or []):
                if str(tool_call.get("name") or "") != "save_memory":
                    continue
                args = tool_call.get("args", {})
                if isinstance(args, str):
                    args = json.loads(args)
                if isinstance(args, dict):
                    tool_payload = args
                    break

            if not tool_payload:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                return False

            entry = tool_payload.get("history_entry")
            update = tool_payload.get("memory_update")

            if entry:
                if not isinstance(entry, str):
                    entry = json.dumps(entry, ensure_ascii=False)
                self.append_history(entry)
            if update:
                if not isinstance(update, str):
                    update = json.dumps(update, ensure_ascii=False)
                if update != current_memory:
                    self.write_long_term(update)

            session.last_consolidated = 0 if archive_all else len(session.messages) - keep_count
            logger.info(
                "Memory consolidation done: {} messages, last_consolidated={}",
                len(session.messages),
                session.last_consolidated,
            )
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False



