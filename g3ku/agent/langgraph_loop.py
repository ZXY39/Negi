"""Compatibility wrapper for the unified LangGraph agent loop."""

from __future__ import annotations

from typing import Any

from loguru import logger

from g3ku.agent.langgraph_memory import LangGraphMemoryConsolidator
from g3ku.agent.loop import AgentLoop


class LangGraphAgentLoop(AgentLoop):
    """Backward-compatible alias for the single-runtime agent loop."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """Compatibility path kept for tests and legacy patch points."""
        try:
            return await LangGraphMemoryConsolidator(self.workspace).consolidate(
                session,
                self.provider,
                self.model,
                archive_all=archive_all,
                memory_window=self.memory_window,
            )
        except Exception:
            logger.exception("LangGraph memory consolidator failed, falling back to base store")
            return await super()._consolidate_memory(session, archive_all=archive_all)

