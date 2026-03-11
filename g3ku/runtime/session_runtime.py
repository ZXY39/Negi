from __future__ import annotations

from g3ku.runtime.session_agent import RuntimeAgentSession


class LegacyAgentSession(RuntimeAgentSession):
    """Backward-compatible alias for the real runtime session implementation."""


__all__ = ["LegacyAgentSession", "RuntimeAgentSession"]

